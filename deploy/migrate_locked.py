#!/usr/bin/env python3
"""Run alembic upgrade head while holding a MySQL named lock.

GET_LOCK is tied to the session that requested it, so the connection opened
here stays open for the whole migration and is closed only after RELEASE_LOCK.
Two nodes starting together then cannot migrate in parallel: the second waits
until MIGRATE_LOCK_TIMEOUT_SECONDS (default 300) and exits without alembic if
the lock is still held.

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
_TRUE = {"1", "true", "TRUE", "yes", "YES"}


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


def migrate_under_lock(connection, upgrade, timeout, name=LOCK_NAME):
    """Hold `name` for the duration of upgrade().

    Returns 0 after upgrade, or 1 if the lock was not acquired (upgrade is not
    called). RELEASE_LOCK runs either way, including when upgrade raises.
    `connection` must be the same object for the acquire and the release:
    MySQL drops the lock if that session closes.
    """
    try:
        if not acquire_migration_lock(connection, name, timeout):
            print(
                f"Could not acquire MySQL lock {name!r} within {timeout} seconds. "
                "Another node is migrating this database; alembic was not started.",
                file=sys.stderr,
            )
            return 1
        upgrade()
        return 0
    finally:
        release_migration_lock(connection, name)


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
    idle. It never reconnects: a new session would silently lose the lock.
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

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(15)
            async with self._gate:
                async with self._conn.cursor() as cursor:
                    await cursor.execute("SELECT 1")

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
