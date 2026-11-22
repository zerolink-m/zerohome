#!/usr/bin/env python3
"""Create root-only Compose and application bootstrap environment files."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import secrets
import sys
import tempfile


def _check_value(name: str, value: str) -> str:
    value = str(value)
    if not value or any(ch in value for ch in "\x00\r\n"):
        raise ValueError(f"{name} must be non-empty and contain no line breaks")
    return value


def compose_env_quote(value: str) -> str:
    """Single-quote Compose env values so $, #, spaces and quotes stay literal."""
    _check_value("environment value", value)
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render_configs(mode: str, values: dict[str, str]) -> tuple[str, str]:
    if mode not in {"builtin", "external"}:
        raise ValueError("mode must be builtin or external")
    required = {"DB_NAME", "DB_USER", "DB_PASSWORD"}
    if mode == "builtin":
        required.add("MYSQL_ROOT_PASSWORD")
        values = {**values, "DB_HOST": "db", "DB_PORT": "3306"}
    else:
        required.update({"DB_HOST", "DB_PORT"})
    missing = sorted(required - values.keys())
    if missing:
        raise ValueError("missing required configuration values: " + ", ".join(missing))
    checked_keys = required | {"DB_HOST", "DB_PORT"}
    checked = {key: _check_value(key, values[key]) for key in checked_keys}
    deployment = {
        **checked,
        "ZEROHOME_CONFIG": "/etc/zerohome/app.conf",
        "ZEROHOME_BIND": "127.0.0.1",
        "ZEROHOME_IMAGE": "ghcr.io/zerolink-m/zerohome:stable",
    }
    app = {key: checked[key] for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")}
    def render(items: dict[str, str]) -> str:
        return "".join(f"{key}={compose_env_quote(value)}\n" for key, value in sorted(items.items()))
    return render(deployment), render(app)


def write_config_pair(config_dir: Path, mode: str, values: dict[str, str]) -> tuple[Path, Path]:
    deployment_text, app_text = render_configs(mode, values)
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    deployment = config_dir / "deployment.conf"
    app = config_dir / "app.conf"
    if deployment.exists() or app.exists():
        raise FileExistsError("existing ZeroHome config found; refusing to overwrite it")
    temporary_files: list[Path] = []
    linked_files: list[Path] = []
    try:
        for path, content in ((deployment, deployment_text), (app, app_text)):
            fd, temporary = tempfile.mkstemp(prefix=".zerohome-", dir=config_dir)
            temp_path = Path(temporary)
            temporary_files.append(temp_path)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp_path, path)
            linked_files.append(path)
            temp_path.unlink()
            temporary_files.remove(temp_path)
    except Exception:
        for path in temporary_files:
            path.unlink(missing_ok=True)
        for path in linked_files:
            path.unlink(missing_ok=True)
        raise
    return deployment, app


def prompt_values(mode: str) -> dict[str, str]:
    values = {"DB_NAME": input("Database name [zerocams]: ").strip() or "zerocams",
              "DB_USER": input("Database user [zerocams]: ").strip() or "zerocams"}
    if mode == "builtin":
        values["DB_PASSWORD"] = secrets.token_urlsafe(36)
        values["MYSQL_ROOT_PASSWORD"] = secrets.token_urlsafe(36)
        print("Generated strong database credentials; they will be written to root-only config files.")
    else:
        values["DB_HOST"] = input("External MySQL host: ").strip()
        values["DB_PORT"] = input("External MySQL port [3306]: ").strip() or "3306"
        values["DB_PASSWORD"] = getpass.getpass("Database password (input hidden): ")
        confirmation = getpass.getpass("Repeat database password: ")
        if values["DB_PASSWORD"] != confirmation:
            raise RuntimeError("database passwords did not match")
        if not input("Confirm an external-database backup exists and migration is approved (type YES): ").strip() == "YES":
            raise RuntimeError("external DB backup/migration confirmation was not provided")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("builtin", "external"), required=True)
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/zerohome"))
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("interactive terminal required")
    try:
        paths = write_config_pair(args.config_dir, args.mode, prompt_values(args.mode))
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Setup configuration failed: {exc}", file=sys.stderr)
        return 1
    print("Created root-only configuration files:")
    for path in paths:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
