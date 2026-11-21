from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("setup_config", ROOT / "deploy/setup_config.py")
setup_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup_config)


def test_builtin_config_contains_no_external_or_root_password_in_app_file(tmp_path):
    values = {"DB_NAME": "zerocams", "DB_USER": "zero", "DB_PASSWORD": "fixture-db-password",
              "MYSQL_ROOT_PASSWORD": "fixture-root-password"}
    deployment, app = setup_config.render_configs("builtin", values)
    assert "DB_HOST='db'" in deployment
    assert "MYSQL_ROOT_PASSWORD='fixture-root-password'" in deployment
    assert "MYSQL_ROOT_PASSWORD" not in app
    assert "fixture-root-password" not in app
    assert "DB_PASSWORD='fixture-db-password'" in app


def test_external_config_quotes_compose_metacharacters_without_echoing(tmp_path):
    values = {"DB_HOST": "db.example", "DB_PORT": "3307", "DB_NAME": "zero home",
              "DB_USER": "zero", "DB_PASSWORD": "p$ass#word'with\\quote"}
    deployment, app = setup_config.render_configs("external", values)
    assert "DB_HOST='db.example'" in deployment
    assert "DB_PASSWORD='p$ass#word\\'with\\\\quote'" in app
    assert "MYSQL_ROOT_PASSWORD" not in deployment


def test_config_files_are_private_and_never_overwritten(tmp_path):
    paths = setup_config.write_config_pair(tmp_path / "etc", "builtin", {
        "DB_NAME": "zerocams", "DB_USER": "zero", "DB_PASSWORD": "fixture-password",
        "MYSQL_ROOT_PASSWORD": "fixture-root-password",
    })
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    before = paths[0].read_text()
    with pytest.raises(FileExistsError):
        setup_config.write_config_pair(tmp_path / "etc", "builtin", {
            "DB_NAME": "other", "DB_USER": "other", "DB_PASSWORD": "replacement",
            "MYSQL_ROOT_PASSWORD": "replacement-root",
        })
    assert paths[0].read_text() == before


def test_image_contract_copies_operator_admin_bootstrap_into_image():
    dockerfile = (ROOT / "Dockerfile").read_text()
    dockerignore = (ROOT / ".dockerignore").read_text()
    assert "deploy/bootstrap_admin.py ./deploy/bootstrap_admin.py" in dockerfile
    assert "!deploy/bootstrap_admin.py" in dockerignore


def test_setup_documents_the_real_update_paths_and_does_not_remove_volumes():
    source = (ROOT / "deploy/setup.sh").read_text()
    assert "INSTALL_DIR=/opt/zerohome" in source
    assert "CONFIG_DIR=/etc/zerohome" in source
    assert "bash ./deploy/update.sh" in source
    assert "python -m deploy.bootstrap_admin" in source
    assert "docker compose down -v" not in source
    assert "rm -rf" not in source


def test_setup_guards_stopped_projects_and_existing_database_volumes():
    source = (ROOT / "deploy/setup.sh").read_text()
    assert "docker ps -a --filter label=com.docker.compose.project=zerohome" in source
    assert "docker volume ls -q --filter label=com.docker.compose.project=zerohome" in source
    assert "active Compose project 'zerohome' detected" in source
    assert "existing Compose data volumes for 'zerohome' detected" in source


def test_setup_confirms_starting_existing_docker_and_uses_explicit_apt_steps():
    source = (ROOT / "deploy/setup.sh").read_text()
    confirm = source.index('ask_yes "Docker is installed but stopped.')
    enable = source.index("systemctl enable --now docker", confirm)
    assert confirm < enable
    assert "apt-get update\n    apt-get install -y ca-certificates curl gnupg" in source
    assert "apt-get purge" not in source
    assert "apt-get remove" not in source


def test_setup_refuses_to_overwrite_external_systemd_dropin():
    source = (ROOT / "deploy/setup.sh").read_text()
    assert '"$EXTERNAL_OVERRIDE"' in source
    preflight = source.index('for path in "$CONFIG_DIR/deployment.conf"')
    preflight_guard = source.index('[[ ! -e "$path" ]] || fail "$path already exists', preflight)
    write_guard = source.index('[[ ! -e "$EXTERNAL_OVERRIDE" ]] || fail', preflight_guard)
    write_file = source.index('cat > "$EXTERNAL_OVERRIDE"', write_guard)
    assert preflight < preflight_guard < write_guard < write_file
