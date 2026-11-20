"""Compose profiles, node environment, migration lock and external-db updates."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

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
    }
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
        tmp_path, ROOT / "compose.yaml",
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


def test_app_environment_receives_node_and_cluster_settings(tmp_path):
    rendered = _compose_config(
        tmp_path, ROOT / "compose.yaml",
        {
            "MYSQL_ROOT_PASSWORD": "test-root-password",
            "NODE_ID": "node-a",
            "NODE_ADDRESS": "10.1.0.8:4700",
            "CLUSTER_ENABLED": "true",
        },
    )
    env = rendered["services"]["app"]["environment"]
    assert env["NODE_ID"] == "node-a"
    assert env["NODE_ADDRESS"] == "10.1.0.8:4700"
    assert env["CLUSTER_ENABLED"] == "true"
    for name in (
        "CLUSTER_SECRET", "CLUSTER_MEDIA_HOST", "CLUSTER_MEDIA_PORT",
        "STORAGE_BACKEND", "S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY",
        "S3_SECRET_KEY", "S3_REGION",
    ):
        assert name in env
    assert env["CLUSTER_MEDIA_HOST"] == "127.0.0.1"
    assert env["CLUSTER_MEDIA_PORT"] == "4700"
    assert env["STORAGE_BACKEND"] == "local"
    source = (ROOT / "compose.yaml").read_text()
    assert "NODE_ID: ${NODE_ID:-}" in source
    assert "NODE_ADDRESS: ${NODE_ADDRESS:-}" in source
    assert 'CLUSTER_ENABLED: "${CLUSTER_ENABLED:-false}"' in source


def test_external_compose_has_no_database_service(tmp_path):
    rendered = _compose_config(
        tmp_path, ROOT / "compose.external-db.yaml",
        {"DB_HOST": "10.9.9.9", "DB_PORT": "3307"},
        profile="maintenance",
    )
    assert set(rendered["services"]) == {"app", "migrate"}
    assert "db" not in rendered["services"]
    app = rendered["services"]["app"]
    migrate = rendered["services"]["migrate"]
    assert app["environment"]["DB_HOST"] == "10.9.9.9"
    assert app["environment"]["DB_PORT"] == "3307"
    assert app["environment"]["NODE_ID"] == ""
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
            assert call["args"][4].endswith("compose.external-db.yaml")
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
