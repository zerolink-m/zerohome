"""Offline release/privacy and migration-order regression tests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_visibility", ROOT / "deploy/check-package-visibility.py")
visibility = importlib.util.module_from_spec(spec)
spec.loader.exec_module(visibility)


def test_public_package_can_be_published(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    calls = []

    def get(path, token):
        calls.append((path, token))
        return {"visibility": "public"}

    monkeypatch.setattr(visibility, "get", get)
    visibility.verify()
    assert calls == [(visibility.PACKAGE, "test-token")]


@pytest.mark.parametrize("value", ["private", "internal", None, "PUBLIC", 1])
def test_non_public_or_unknown_package_is_never_published(monkeypatch, value):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr(visibility, "get", lambda path, token: {"visibility": value})
    with pytest.raises(SystemExit):
        visibility.verify()


def test_missing_github_token_fails_closed(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)

    def unexpected_get(*args):
        pytest.fail("GitHub API must not be called without GH_TOKEN")

    monkeypatch.setattr(visibility, "get", unexpected_get)
    with pytest.raises(SystemExit, match="GH_TOKEN is required"):
        visibility.verify()


@pytest.mark.parametrize("status", [403, 404, 500])
def test_visibility_readback_fails_closed(monkeypatch, status):
    monkeypatch.setenv("GH_TOKEN", "test-token")

    def get(path, token):
        assert path == visibility.PACKAGE
        assert token == "test-token"
        raise urllib.error.HTTPError("url", status, "error", {}, None)
    monkeypatch.setattr(visibility, "get", get)
    with pytest.raises(SystemExit):
        visibility.verify()


def test_container_does_not_export_runtime_version_environment():
    result = subprocess.run([
        "sh", str(ROOT / "deploy/container-entrypoint.sh"), sys.executable,
        "-c", "import os; from app.version import APP_VERSION; assert 'VERSION' not in os.environ; print(APP_VERSION)",
    ], env={**os.environ, "VERSION": "v0.0.0-stale",
            "PYTHONPATH": str(ROOT)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "4.8.0"


FAKE_DOCKER = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as stream:
    stream.write(json.dumps({"args": args, "image": os.environ.get("ZEROHOME_IMAGE")}) + "\n")
mode = os.environ["TEST_MODE"]
if args[:2] == ["image", "inspect"]:
    print("registry/image@sha256:1234" if "RepoDigests" in args[3] else "new-image")
elif args[0] == "inspect":
    print("healthy" if "State" in args[2] else ("new-image" if mode == "unchanged" else "old-image"))
elif args[0] == "compose":
    args = args[5:]
    if args[0] == "config":
        print(json.dumps({"services": {"app": {"image": "registry/image:stable"}}}))
    elif args[0] == "ps":
        print("app-container")
    elif args[0] == "exec":
        if mode == "backup-fail": sys.exit(1)
        print("-- test database backup")
    elif args[0] == "run" and mode == "migration-fail":
        sys.exit(1)
'''


@pytest.mark.parametrize("mode", ["unchanged", "updated", "backup-fail", "migration-fail"])
def test_update_orders_backup_migration_and_app_and_never_updates_mysql(tmp_path, mode):
    fake = tmp_path / "docker"
    fake.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
    fake.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    backup_dir = tmp_path / "backups"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "CALL_LOG": str(log), "TEST_MODE": mode,
           "ZEROHOME_BACKUP_DIR": str(backup_dir)}
    env.pop("ZEROHOME_IMAGE", None)
    result = subprocess.run(["bash", str(ROOT / "deploy/update.sh")], env=env,
                            capture_output=True, text=True)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    commands = [c["args"][5:] for c in calls if c["args"][0] == "compose"]
    assert not any(c["args"][:2] == ["pull", "db"] for c in calls)
    if mode == "unchanged":
        assert result.returncode == 0, result.stderr
        assert not any(c[0] in ("stop", "run", "up", "exec") for c in commands)
        return
    stop = next(i for i, c in enumerate(commands) if c[0] == "stop")
    backup = next(i for i, c in enumerate(commands) if c[0] == "exec")
    assert stop < backup
    db_up = next(c for c in commands if c[0] == "up" and c[-1] == "db")
    assert "--no-recreate" in db_up and db_up[db_up.index("--pull") + 1] == "never"
    app_up = [c for c in commands if c[0] == "up" and c[-1] == "app"]
    migrations = [i for i, c in enumerate(commands) if c[0] == "run"]
    if mode == "backup-fail":
        assert not migrations and not app_up and result.returncode != 0
        assert not list(backup_dir.glob("*.sql.gz"))
    else:
        assert backup < migrations[0]
        assert list(backup_dir.glob("*.sql.gz"))
        assert bool(app_up) == (mode == "updated")
        assert (result.returncode == 0) == (mode == "updated"), result.stderr
        if app_up:
            assert migrations[0] < commands.index(app_up[0])
        for c in calls:
            if c["args"][0] == "compose" and c["args"][5] in ("run", "up"):
                assert c["image"] == "registry/image@sha256:1234"
