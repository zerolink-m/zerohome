#!/usr/bin/env python3
"""Run alembic upgrade head while holding a MySQL named lock.

GET_LOCK is tied to the session that requested it, so the connection opened
here stays open for the whole migration and is closed only after RELEASE_LOCK.
Two nodes starting together then cannot migrate in parallel: the second waits
until MIGRATE_LOCK_TIMEOUT_SECONDS (default 300) and exits without alembic if
the lock is still held. If the lock session dies while alembic is running,
MySQL drops the lock; this process then stops alembic and exits non-zero
instead of leaving a second node free to migrate the same database.

The database host, port, user, password and name come from app settings, the
same fields settings.database_url is built from. The script does not read or
parse a URL out of the environment.
"""
import asyncio
import os
import sys
import threading
from pathlib import Path

LOCK_NAME = "zerohome_migrate"
DEFAULT_LOCK_TIMEOUT_SECONDS = 300
GET_LOCK_SQL = "SELECT GET_LOCK(%s, %s)"
RELEASE_LOCK_SQL = "SELECT RELEASE_LOCK(%s)"
# Keepalive on the lock session. Between pings the socket is polled so a TCP
# close aborts alembic without waiting out this whole interval.
HEARTBEAT_INTERVAL_SECONDS = 15
LOCK_WATCH_INTERVAL_SECONDS = 0.2
_TRUE = {"1", "true", "TRUE", "yes", "YES"}


class LockLost(BaseException):
    """Injected into the alembic thread when the GET_LOCK session dies.

    BaseException, not Exception, so a broad ``except Exception`` inside
    alembic does not swallow the abort and keep migrating without the lock.
    """


def lock_timeout_seconds(raw=None):
    """Seconds to wait for the lock. Negative would make MySQL wait forever."""
    if raw is None:
        raw = os.environ.get("MIGRATE_LOCK_TIMEOUT_SECONDS", "")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_LOCK_TIMEOUT_SECONDS
    try:
        timeout = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise SystemExit(
            f"MIGRATE_LOCK_TIMEOUT_SECONDS must be an integer number of seconds, got {raw!r}."
        ) from exc
    if timeout < 0:
        raise SystemExit(
            "MIGRATE_LOCK_TIMEOUT_SECONDS must be >= 0. "
            "A negative value would make MySQL GET_LOCK wait forever."
        )
    return timeout


def lock_granted(row):
    """True only when GET_LOCK returns 1. 0 is a timeout, NULL is an error."""
    if row is None or isinstance(row, (bool, int, float, str, bytes)):
        value = row
    else:
        try:
            value = row[0]
        except (TypeError, KeyError, IndexError):
            return False
    if value is None or isinstance(value, bool):
        return False
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return False


def acquire_migration_lock(connection, name, timeout):
    """Ask MySQL for `name` on this connection. Return True only when granted."""
    row = connection.execute(GET_LOCK_SQL, (name, int(timeout)))
    return lock_granted(row)


def release_migration_lock(connection, name):
    """Release `name` if this connection holds it. Harmless if it does not."""
    connection.execute(RELEASE_LOCK_SQL, (name,))


def _report_lock_lost(connection, name):
    reason = getattr(connection, "lock_lost_reason", "") or "the lock connection stopped answering"
    print(
        f"MySQL lock {name!r} was lost during migration ({reason}). "
        "alembic was stopped so another node cannot migrate this database at the same time. "
        "The schema may be only partly applied; fix it before starting the application.",
        file=sys.stderr,
    )


def _raise_in_thread(thread, exc_type):
    """Ask `thread` to raise `exc_type` at the next Python bytecode."""
    ident = thread.ident
    if not ident:
        return
    import ctypes

    set_async_exc = ctypes.pythonapi.PyThreadState_SetAsyncExc
    set_async_exc.argtypes = [ctypes.c_ulong, ctypes.py_object]
    set_async_exc.restype = ctypes.c_int
    count = set_async_exc(ctypes.c_ulong(ident), ctypes.py_object(exc_type))
    if count > 1:
        set_async_exc(ctypes.c_ulong(ident), None)
        raise SystemError(f"PyThreadState_SetAsyncExc affected {count} threads")


def _run_upgrade_while_locked(connection, upgrade, name):
    """Run upgrade() until it finishes or `connection.lock_lost` is set.

    A connection without `lock_lost` runs upgrade on this thread, which is what
    the existing fakes do. The real lock connection always has the event: the
    upgrade then runs on a side thread so this thread can interrupt it. The
    side thread is joined before returning, so the caller can RELEASE_LOCK
    only after alembic has stopped.
    """
    lost = getattr(connection, "lock_lost", None)
    if lost is None:
        upgrade()
        return 0
    if lost.is_set():
        _report_lock_lost(connection, name)
        return 1

    outcome = {}

    def run():
        try:
            upgrade()
        except BaseException as exc:
            outcome["exc"] = exc
        else:
            outcome["ok"] = True

    worker = threading.Thread(target=run, name="zerohome-alembic-upgrade")
    worker.start()
    aborted = False
    try:
        while worker.is_alive():
            if lost.is_set():
                aborted = True
                _raise_in_thread(worker, LockLost)
            worker.join(0.05)
    except KeyboardInterrupt:
        _raise_in_thread(worker, KeyboardInterrupt)
        worker.join()
        raise
    finally:
        if worker.is_alive() and lost.is_set():
            aborted = True
            _raise_in_thread(worker, LockLost)
        # Do not release the lock while alembic is still running.
        worker.join()
    if aborted or isinstance(outcome.get("exc"), LockLost):
        extra = outcome.get("exc")
        if extra is not None and not isinstance(extra, LockLost):
            print(
                f"Migration also raised {type(extra).__name__}: {extra}",
                file=sys.stderr,
            )
        _report_lock_lost(connection, name)
        return 1
    exc = outcome.get("exc")
    if exc is not None:
        raise exc
    return 0


def migrate_under_lock(connection, upgrade, timeout, name=LOCK_NAME):
    """Hold `name` for the duration of upgrade().

    Returns 0 after upgrade, or 1 if the lock was not acquired (upgrade is not
    called) or the lock session died while upgrade was running. RELEASE_LOCK
    runs either way, including when upgrade raises. `connection` must be the
    same object for the acquire and the release: MySQL drops the lock if that
    session closes. A connection that exposes `lock_lost` (a threading.Event)
    aborts upgrade when that event is set, so alembic does not continue after
    MySQL has already given the lock away.
    """
    try:
        if not acquire_migration_lock(connection, name, timeout):
            print(
                f"Could not acquire MySQL lock {name!r} within {timeout} seconds. "
                "Another node is migrating this database; alembic was not started.",
                file=sys.stderr,
            )
            return 1
        return _run_upgrade_while_locked(connection, upgrade, name)
    finally:
        try:
            release_migration_lock(connection, name)
        except Exception as exc:
            # The session is already gone, so MySQL has dropped the lock. Do not
            # hide the abort behind this secondary error.
            lost = getattr(connection, "lock_lost", None)
            if lost is None or not lost.is_set():
                raise
            print(
                f"Could not release MySQL lock {name!r} after it was lost: {exc}",
                file=sys.stderr,
            )


def app_root():
    """Image workdir when baked in, otherwise the checkout that holds this file."""
    image_root = Path("/app")
    if (image_root / "alembic.ini").is_file() and (image_root / "app").is_dir():
        return image_root
    return Path(__file__).resolve().parents[1]


def run_alembic_upgrade():
    root = app_root()
    sys.path.insert(0, str(root))
    os.chdir(root)
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(root / "alembic.ini")), "head")


class MysqlLockConnection:
    """asyncmy connection kept on its own loop so GET_LOCK outlives alembic.

    Alembic's env.py calls asyncio.run, so the lock cannot live on that loop.
    A heartbeat runs SELECT 1 on this same connection while it is otherwise
    idle, and the socket is checked between heartbeats. It never reconnects:
    a new session would silently lose the lock. A failed heartbeat or a closed
    socket sets `lock_lost`; the exception is not left unread on the task.
    """

    def __init__(self, host, port, user, password, database, read_timeout):
        import asyncmy

        self._asyncmy = asyncmy
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="zerohome-migrate-lock", daemon=True
        )
        self._conn = None
        self._gate = None
        self._heartbeat = None
        # Created before the lock thread starts. The heartbeat sets it; nothing
        # else reads an unread task exception.
        self.lock_lost = threading.Event()
        self.lock_lost_reason = ""
        self._thread.start()
        try:
            self._submit(self._open(host, int(port), user, password, database, int(read_timeout)))
        except Exception:
            self.close()
            raise

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _open(self, host, port, user, password, database, read_timeout):
        self._gate = asyncio.Lock()
        self._conn = await self._asyncmy.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            db=database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=10,
            read_timeout=read_timeout,
            program_name="zerohome-migrate",
        )
        self._heartbeat = asyncio.create_task(self._heartbeat_loop())
        # If the task dies without the loop catching it, the exception is still
        # read here. An unread task exception used to drop the lock unnoticed.
        self._heartbeat.add_done_callback(self._on_heartbeat_done)

    def _note_lock_lost(self, exc):
        if not self.lock_lost_reason:
            text = str(exc).strip() if exc is not None else ""
            if not text and exc is not None:
                text = type(exc).__name__
            self.lock_lost_reason = text or "lock connection lost"
        self.lock_lost.set()

    def _on_heartbeat_done(self, task):
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            self._note_lock_lost(exc)

    def _lock_socket_closed(self):
        """True when this session can no longer hold GET_LOCK.

        asyncmy leaves Connection.connected True until the next command. The
        protocol is marked as soon as the transport drops, which is the signal
        that MySQL has already released the lock.
        """
        conn = self._conn
        if conn is None:
            return True
        proto = getattr(conn, "_proto", None)
        if proto is not None and (
            getattr(proto, "eof", False) or getattr(proto, "exc", None) is not None
        ):
            return True
        return not getattr(conn, "connected", True)

    async def _ping_lock_connection(self):
        async with self._gate:
            conn = self._conn
            if conn is None:
                raise ConnectionError("migration lock connection is closed")
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1")

    async def _heartbeat_loop(self):
        try:
            loop = asyncio.get_running_loop()
            next_ping = loop.time() + HEARTBEAT_INTERVAL_SECONDS
            while True:
                await asyncio.sleep(LOCK_WATCH_INTERVAL_SECONDS)
                if self._lock_socket_closed():
                    raise ConnectionError("migration lock connection closed")
                if loop.time() >= next_ping:
                    next_ping = loop.time() + HEARTBEAT_INTERVAL_SECONDS
                    await self._ping_lock_connection()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._note_lock_lost(exc)

    def execute(self, sql, params=()):
        return self._submit(self._query(sql, params))

    async def _query(self, sql, params):
        async with self._gate:
            async with self._conn.cursor() as cursor:
                await cursor.execute(sql, params)
                return await cursor.fetchone()

    def close(self):
        try:
            if self._conn is not None or self._heartbeat is not None:
                asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=30)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)

    async def _shutdown(self):
        task = self._heartbeat
        self._heartbeat = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        conn = self._conn
        self._conn = None
        if conn is not None:
            await conn.ensure_closed()


def open_lock_connection():
    """Connect with the app's DB_* settings. The session stays open for the caller."""
    root = app_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from app.core.config import settings

    timeout = lock_timeout_seconds()
    return MysqlLockConnection(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        # The server answers GET_LOCK only when the wait ends, so the client
        # read must outlast that wait or the session (and the lock) drops.
        read_timeout=timeout + 120,
    )


def wait_only(argv):
    if "--wait" in argv:
        return True
    return os.environ.get("ZEROHOME_MIGRATE_WAIT", "") in _TRUE


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    unknown = [arg for arg in args if arg != "--wait"]
    if unknown:
        print("usage: migrate_locked.py [--wait]", file=sys.stderr)
        return 2
    timeout = lock_timeout_seconds()
    connection = open_lock_connection()
    try:
        if wait_only(args):
            print(
                "Waiting for the migration lock. This node will not change the schema.",
                file=sys.stderr,
            )
            return migrate_under_lock(connection, lambda: None, timeout)
        print(f"Acquiring MySQL lock {LOCK_NAME!r} (timeout {timeout}s) and migrating.", file=sys.stderr)
        return migrate_under_lock(connection, run_alembic_upgrade, timeout)
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
