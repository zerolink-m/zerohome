"""Compose profiles, node environment, migration lock and external-db updates."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "migrate_locked", ROOT / "deploy" / "migrate_locked.py"
)
migrate_locked = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migrate_locked)

from tests.test_deployment import FAKE_DOCKER


def _if_bodies(source, header):
    """Every if/fi whose header line contains `header`, including else branches."""
    lines = source.splitlines()
    bodies = []
    for start, line in enumerate(lines):
        if header not in line:
            continue
        depth = 0
        body = []
        for candidate in lines[start:]:
            stripped = candidate.strip()
            if stripped.startswith("if "):
                depth += 1
            body.append(candidate)
            if stripped == "fi":
                depth -= 1
                if depth == 0:
                    bodies.append(body)
                    break
        else:
            raise AssertionError(f"no closing fi for {header!r}")
    return bodies


def _compose_config(tmp_path, compose_file, extra, profile=None):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        # Do not load the checkout .env (it is a secret file, not a fixture).
        "COMPOSE_DISABLE_ENV_FILE": "1",
        "DB_PASSWORD": "test-db-password",
        "DB_NAME": "zerocams",
        "DB_USER": "zerocams",
        # env_file defaults to /etc/zerohome/app.conf, which exists on the
        # deploy host but not in CI. Compose checks the file is present even
        # with --no-env-resolution, so point it at an empty fixture.
        "ZEROHOME_CONFIG": str(tmp_path / "app.conf"),
    }
    (tmp_path / "app.conf").write_text("")
    env.update(extra)
    command = [
        "docker", "compose",
        "--project-directory", str(tmp_path),
        "--env-file", "/dev/null",
        "-f", str(compose_file),
        "config", "--no-env-resolution", "--format", "json",
    ]
    if profile:
        command[2:2] = ["--profile", profile]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_builtin_compose_has_db_migrate_and_app(tmp_path):
    rendered = _compose_config(
        tmp_path, ROOT / "templates" / "compose.yaml",
        {"MYSQL_ROOT_PASSWORD": "test-root-password"},
        profile="maintenance",
    )
    assert set(rendered["services"]) == {"db", "migrate", "app"}
    assert rendered["services"]["migrate"].get("profiles") == ["maintenance"]
    command = rendered["services"]["migrate"].get("command")
    assert command == ["python", "/app/deploy/migrate_locked.py"]
    app = rendered["services"]["app"]["environment"]
    assert app["DB_HOST"] == "db"
    assert app["DB_PORT"] == "3306"


@pytest.mark.parametrize("template", ["compose.yaml", "compose.external-db.yaml"])
def test_app_compose_keeps_identity_and_only_bootstrap_environment(tmp_path, template):
    compose = ROOT / "templates" / template
    rendered = _compose_config(
        tmp_path, compose,
        {"MYSQL_ROOT_PASSWORD": "test-root-password", "DB_HOST": "db.example"},
    )
    env = rendered["services"]["app"]["environment"]
    assert env["DB_HOST"] == ("db" if template == "compose.yaml" else "db.example")
    assert env["HOST"] == "0.0.0.0"
    assert env["PORT"] == "8000"
    assert env["CACHE_DIR"] == "/data/cache"
    assert env["MOTION_FILES_DIR"] == "/data/motion_files"
    forbidden = {
        "NODE_ID", "NODE_ADDRESS", "CLUSTER_ENABLED", "CLUSTER_SECRET",
        "CLUSTER_MEDIA_HOST", "CLUSTER_MEDIA_PORT", "STORAGE_BACKEND",
        "S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_REGION",
        "MIGRATE_LOCK_TIMEOUT_SECONDS",
    }
    assert forbidden.isdisjoint(env)
    assert "VERSION" not in env
    app_volumes = rendered["services"]["app"]["volumes"]
    identity_mounts = [mount for mount in app_volumes if isinstance(mount, dict)
                       and mount.get("target") == "/data/zerocams"]
    assert len(identity_mounts) == 1 and identity_mounts[0].get("type") == "volume"
    identity_source = identity_mounts[0].get("source")
    other_named_volumes = {mount.get("source") for mount in app_volumes if isinstance(mount, dict)
                           and mount.get("target") in {"/data/motion_files", "/data/models"}}
    assert identity_source not in other_named_volumes
    assert "identity" in rendered["volumes"]


def test_external_compose_has_no_database_service(tmp_path):
    rendered = _compose_config(
        tmp_path, ROOT / "templates" / "compose.external-db.yaml",
        {"DB_HOST": "10.9.9.9", "DB_PORT": "3307"},
        profile="maintenance",
    )
    assert set(rendered["services"]) == {"app", "migrate"}
    assert "db" not in rendered["services"]
    app = rendered["services"]["app"]
    migrate = rendered["services"]["migrate"]
    assert app["environment"]["DB_HOST"] == "10.9.9.9"
    assert app["environment"]["DB_PORT"] == "3307"
    assert "depends_on" not in app
    assert "depends_on" not in migrate
    assert migrate.get("profiles") == ["maintenance"]
    assert "mysql" not in rendered.get("volumes", {})


class FakeLockConnection:
    def __init__(self, granted):
        self.granted = granted
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "GET_LOCK" in sql:
            return (1 if self.granted else 0,)
        if "RELEASE_LOCK" in sql:
            return (1,)
        raise AssertionError(sql)


def test_migration_lock_grant_runs_upgrade_and_releases():
    connection = FakeLockConnection(granted=True)
    ran = []
    code = migrate_locked.migrate_under_lock(connection, lambda: ran.append("upgrade"), 300)
    assert code == 0
    assert ran == ["upgrade"]
    statements = [sql for sql, _params in connection.calls]
    assert statements[0] == migrate_locked.GET_LOCK_SQL
    assert statements[-1] == migrate_locked.RELEASE_LOCK_SQL
    assert connection.calls[0][1] == (migrate_locked.LOCK_NAME, 300)


def test_migration_lock_refusal_skips_upgrade_and_still_releases():
    connection = FakeLockConnection(granted=False)
    ran = []
    code = migrate_locked.migrate_under_lock(connection, lambda: ran.append("upgrade"), 300)
    assert code == 1
    assert ran == []
    statements = [sql for sql, _params in connection.calls]
    assert any("GET_LOCK" in sql for sql in statements)
    assert statements[-1] == migrate_locked.RELEASE_LOCK_SQL


def test_migration_lock_is_released_when_upgrade_raises():
    connection = FakeLockConnection(granted=True)

    def boom():
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        migrate_locked.migrate_under_lock(connection, boom, 5)
    assert connection.calls[-1][0] == migrate_locked.RELEASE_LOCK_SQL


class DyingLockConnection(FakeLockConnection):
    """Fake GET_LOCK session whose death the migrator can observe."""

    def __init__(self):
        super().__init__(granted=True)
        self.lock_lost = threading.Event()
        self.lock_lost_reason = ""

    def die(self):
        self.lock_lost_reason = "server has gone away"
        self.lock_lost.set()


def test_live_lock_connection_still_runs_upgrade():
    connection = DyingLockConnection()
    ran = []
    code = migrate_locked.migrate_under_lock(connection, lambda: ran.append("upgrade"), 5)
    assert code == 0
    assert ran == ["upgrade"]
    assert connection.calls[0][0] == migrate_locked.GET_LOCK_SQL
    assert connection.calls[-1][0] == migrate_locked.RELEASE_LOCK_SQL


def test_live_lock_connection_still_propagates_upgrade_errors():
    connection = DyingLockConnection()

    def boom():
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        migrate_locked.migrate_under_lock(connection, boom, 5)
    assert connection.calls[-1][0] == migrate_locked.RELEASE_LOCK_SQL


def test_already_lost_lock_does_not_start_upgrade(capsys):
    connection = DyingLockConnection()
    connection.die()
    ran = []
    code = migrate_locked.migrate_under_lock(connection, lambda: ran.append("upgrade"), 5)
    assert code == 1
    assert ran == []
    assert connection.calls[-1][0] == migrate_locked.RELEASE_LOCK_SQL
    assert "was lost during migration" in capsys.readouterr().err


def test_lock_connection_death_aborts_upgrade(capsys):
    connection = DyingLockConnection()
    started = threading.Event()
    finished = []

    def upgrade():
        started.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.01)
        finished.append("completed")

    def kill():
        assert started.wait(2)
        connection.die()

    threading.Thread(target=kill, daemon=True).start()
    code = migrate_locked.migrate_under_lock(connection, upgrade, 30)
    assert code == 1
    assert started.is_set()
    assert finished == []
    statements = [sql for sql, _params in connection.calls]
    assert statements[0] == migrate_locked.GET_LOCK_SQL
    assert statements[-1] == migrate_locked.RELEASE_LOCK_SQL
    err = capsys.readouterr().err
    assert "was lost during migration" in err
    assert "server has gone away" in err


def _bare_lock_holder():
    holder = migrate_locked.MysqlLockConnection.__new__(migrate_locked.MysqlLockConnection)
    holder.lock_lost = threading.Event()
    holder.lock_lost_reason = ""
    holder._conn = None
    holder._gate = None
    return holder


def test_closed_lock_socket_marks_lock_lost(monkeypatch):
    monkeypatch.setattr(migrate_locked, "LOCK_WATCH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(migrate_locked, "HEARTBEAT_INTERVAL_SECONDS", 30)
    holder = _bare_lock_holder()
    holder._conn = SimpleNamespace(connected=True, _proto=SimpleNamespace(eof=True, exc=None))

    asyncio.run(holder._heartbeat_loop())

    assert holder.lock_lost.is_set()
    assert "closed" in holder.lock_lost_reason


def test_heartbeat_ping_failure_marks_lock_lost(monkeypatch):
    monkeypatch.setattr(migrate_locked, "LOCK_WATCH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(migrate_locked, "HEARTBEAT_INTERVAL_SECONDS", 0)
    holder = _bare_lock_holder()

    class Boom:
        connected = True
        _proto = SimpleNamespace(eof=False, exc=None)

        def cursor(self):
            raise ConnectionError("server has gone away")

    async def drive():
        holder._gate = asyncio.Lock()
        holder._conn = Boom()
        await holder._heartbeat_loop()

    asyncio.run(drive())
    assert holder.lock_lost.is_set()
    assert "gone away" in holder.lock_lost_reason


def test_heartbeat_cancel_is_not_lock_loss():
    holder = _bare_lock_holder()
    holder._conn = SimpleNamespace(connected=True, _proto=SimpleNamespace(eof=False, exc=None))

    async def drive():
        task = asyncio.create_task(holder._heartbeat_loop())
        task.add_done_callback(holder._on_heartbeat_done)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert holder.lock_lost.is_set() is False


def test_heartbeat_task_failure_is_not_left_unread():
    holder = _bare_lock_holder()

    async def boom():
        raise ConnectionError("server has gone away")

    async def drive():
        task = asyncio.create_task(boom())
        task.add_done_callback(holder._on_heartbeat_done)
        with pytest.raises(ConnectionError):
            await task

    asyncio.run(drive())
    assert holder.lock_lost.is_set()
    assert "gone away" in holder.lock_lost_reason


def test_null_get_lock_is_not_a_grant():
    class NullConnection:
        def execute(self, sql, params=()):
            return (None,)

    assert migrate_locked.acquire_migration_lock(NullConnection(), "zerohome_migrate", 1) is False


def test_lock_timeout_rejects_negative_and_garbage():
    assert migrate_locked.lock_timeout_seconds("") == 300
    assert migrate_locked.lock_timeout_seconds("15") == 15
    with pytest.raises(SystemExit):
        migrate_locked.lock_timeout_seconds("-1")
    with pytest.raises(SystemExit):
        migrate_locked.lock_timeout_seconds("soon")


def test_lock_script_uses_settings_fields_not_database_url():
    source = (ROOT / "deploy" / "migrate_locked.py").read_text()
    assert 'environ["DATABASE_URL"]' not in source
    assert 'environ.get("DATABASE_URL"' not in source
    assert "make_url" not in source
    assert "urlsplit" not in source
    for field in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"):
        assert f"settings.{field}" in source


def _run_update(tmp_path, extra):
    fake = tmp_path / "docker"
    fake.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
    fake.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "CALL_LOG": str(log),
        "TEST_MODE": "updated",
        "ZEROHOME_BACKUP_DIR": str(tmp_path / "backups"),
        **extra,
    }
    env.pop("ZEROHOME_IMAGE", None)
    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / "update.sh")],
        env=env, capture_output=True, text=True,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    commands = [call["args"][5:] for call in calls if call["args"][0] == "compose"]
    return result, calls, commands


def test_update_external_db_branch_does_not_call_mysqldump():
    source = (ROOT / "deploy" / "update.sh").read_text()
    bodies = _if_bodies(source, 'if [[ "$external" == 1 ]]; then')
    body = next(candidate for candidate in bodies if any("refusing automatic backup" in line for line in candidate))
    else_at = next(i for i, line in enumerate(body) if line.strip() == "else")
    external_lines = body[:else_at]
    builtin_lines = body[else_at:]
    assert any("refusing automatic backup" in line for line in external_lines)
    assert not any("exec -T db" in line or "mysqldump --protocol" in line for line in external_lines)
    assert any("mysqldump --protocol" in line for line in builtin_lines)
    follower = _if_bodies(source, 'if [[ "$migrate_role" == 0 ]]; then')[0]
    assert not any("mysqldump" in line for line in follower)


def test_update_external_db_does_not_dump_and_still_migrates(tmp_path):
    result, calls, commands = _run_update(tmp_path, {"ZEROHOME_EXTERNAL_DB": "1"})
    assert result.returncode == 0, result.stderr
    for call in calls:
        if call["args"][0] == "compose":
            assert call["args"][4].endswith("templates/compose.external-db.yaml")
    flat = [" ".join(command) for command in commands]
    assert not any(command[0] == "exec" for command in commands)
    assert not any("mysqldump" in text for text in flat)
    assert not any(command[0] == "up" and command[-1] == "db" for command in commands)
    assert "refusing automatic backup" in result.stdout
    migrations = [command for command in commands if command[0] == "run"]
    assert len(migrations) == 1
    assert "ZEROHOME_MIGRATE_WAIT" not in " ".join(migrations[0])
    assert not list((tmp_path / "backups").glob("*.sql.gz"))
    app_up = next(command for command in commands if command[0] == "up" and command[-1] == "app")
    assert commands.index(migrations[0]) < commands.index(app_up)


def test_update_follower_waits_and_does_not_migrate(tmp_path):
    result, calls, commands = _run_update(tmp_path, {
        "ZEROHOME_EXTERNAL_DB": "1",
        "ZEROHOME_MIGRATE": "0",
    })
    assert result.returncode == 0, result.stderr
    flat = [" ".join(command) for command in commands]
    assert not any("mysqldump" in text or command[0] == "exec" for command, text in zip(commands, flat))
    assert not any(command[0] == "up" and command[-1] == "db" for command in commands)
    runs = [command for command in commands if command[0] == "run"]
    assert len(runs) == 1
    assert "ZEROHOME_MIGRATE_WAIT=1" in runs[0]
    stop = next(i for i, command in enumerate(commands) if command[0] == "stop")
    assert commands.index(runs[0]) < stop
    app_up = next(i for i, command in enumerate(commands) if command[0] == "up" and command[-1] == "app")
    assert stop < app_up
