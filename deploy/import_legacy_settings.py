#!/usr/bin/env python3
"""Explicit, one-time import of legacy dotenv runtime settings into the DB."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import dotenv_values
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, settings
from app.models.node import Node
from app.models.runtime_config import GlobalSetting, NodeSetting, SystemSettings
from app.services.cluster.identity import get_node_id
from app.services.runtime_configuration import global_fields, node_fields


class ImportFailure(Exception):
    """Safe-to-display import error; never includes a source value."""


def _parse_source(source: Path) -> dict[str, str]:
    if not source.is_file():
        raise ImportFailure("Source must be a readable regular file")
    try:
        parsed = dotenv_values(source, interpolate=False, encoding="utf-8")
    except Exception:
        raise ImportFailure("Could not parse source file") from None
    return {key: value for key, value in parsed.items() if key and value is not None}


def _validated_values(
    raw: dict[str, str], stored_values: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object], int, int]:
    global_keys = global_fields()
    node_keys = node_fields()
    accepted_global = {key: value for key, value in raw.items() if key in global_keys}
    accepted_node = {key: value for key, value in raw.items() if key in node_keys}
    skipped = len(set(raw) - set(accepted_global) - set(accepted_node))
    if not accepted_global and not accepted_node:
        return {}, {}, skipped, len(raw)

    candidate = {**settings.model_dump(), **(stored_values or {}), **accepted_global, **accepted_node}
    try:
        validated = Settings(_env_file=None, **candidate)
    except (ValidationError, ValueError, TypeError):
        raise ImportFailure("Invalid runtime setting data; no values were written") from None
    return (
        {key: getattr(validated, key) for key in accepted_global},
        {key: getattr(validated, key) for key in accepted_node},
        skipped,
        len(raw),
    )


async def import_legacy_settings(
    db: AsyncSession, source: Path, *, apply: bool, node_id: str | None = None,
) -> dict[str, object]:
    """Validate and optionally persist explicitly selected runtime fields."""
    raw = _parse_source(source)
    current_node_id = node_id or get_node_id()

    system = await db.scalar(select(SystemSettings).where(SystemSettings.id == 1).with_for_update())
    if system is None:
        raise ImportFailure(
            "Runtime configuration is not initialized; start the application once, then rerun the import"
        )
    if system.revision != 0:
        raise ImportFailure(
            "Import refused: system_settings.revision is not 0; the runtime configuration has already been edited"
        )
    node = await db.get(Node, current_node_id)
    if node is None:
        raise ImportFailure(
            "Current node is not registered; start the application once to register it, then rerun the import"
        )
    identity = (node.configuration or {}).get("identity_id")
    if identity and identity != current_node_id:
        raise ImportFailure("Persisted node identity does not match the registered node")

    global_rows = (await db.execute(select(GlobalSetting))).scalars().all()
    node_rows = (await db.execute(select(NodeSetting).where(NodeSetting.node_id == current_node_id))).scalars().all()
    stored_values = {row.key: row.value for row in global_rows}
    stored_values.update({row.key: row.value for row in node_rows})
    globals_to_import, nodes_to_import, skipped, parsed_count = _validated_values(raw, stored_values)

    if apply and (globals_to_import or nodes_to_import):
        now = int(time.time())
        for key, value in globals_to_import.items():
            row = await db.get(GlobalSetting, key)
            if row is None:
                db.add(GlobalSetting(key=key, value=value))
            else:
                row.value = value
        for key, value in nodes_to_import.items():
            row = await db.get(NodeSetting, (current_node_id, key))
            if row is None:
                db.add(NodeSetting(node_id=current_node_id, key=key, value=value))
            else:
                row.value = value
        system.revision = 1
        system.updated_at = now
        await db.commit()

    return {
        "parsed": parsed_count,
        "global": len(globals_to_import),
        "node": len(nodes_to_import),
        "skipped": skipped,
        "global_keys": sorted(globals_to_import),
        "node_keys": sorted(nodes_to_import),
    }


def _database_url_from_process_environment() -> tuple[URL, int, int]:
    required = ("DB_HOST", "DB_USER", "DB_NAME")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ImportFailure("Missing process database environment: " + ", ".join(missing))
    host = os.environ["DB_HOST"]
    user = os.environ["DB_USER"]
    password = os.environ.get("DB_PASSWORD", "")
    database = os.environ["DB_NAME"]
    try:
        port = int(os.environ.get("DB_PORT", "3306"))
        read_timeout = int(os.environ.get("DB_READ_TIMEOUT_SECONDS", "30"))
        write_timeout = int(os.environ.get("DB_WRITE_TIMEOUT_SECONDS", "30"))
        if not 1 <= port <= 65535 or read_timeout <= 0 or write_timeout <= 0:
            raise ValueError
    except ValueError:
        raise ImportFailure("Invalid process database connection settings") from None
    url = URL.create(
        "mysql+asyncmy", username=user, password=password, host=host, port=port, database=database,
    )
    return url, read_timeout, write_timeout


async def _run_cli(source: Path, apply: bool) -> dict[str, object]:
    url, read_timeout, write_timeout = _database_url_from_process_environment()
    import asyncmy
    import inspect

    connect_args: dict[str, object] = {"read_timeout": read_timeout}
    if "write_timeout" in inspect.signature(asyncmy.connect).parameters:
        connect_args["write_timeout"] = write_timeout
    engine = create_async_engine(url, pool_pre_ping=True, connect_args=connect_args)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with sessions() as db:
            return await import_legacy_settings(db, source, apply=apply)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Explicit legacy dotenv file to import")
    parser.add_argument("--apply", action="store_true", help="Persist the validated values (default: dry-run)")
    args = parser.parse_args()
    try:
        result = asyncio.run(_run_cli(args.source, args.apply))
    except ImportFailure as exc:
        print(f"Import refused: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("Import failed; no setting values were displayed", file=sys.stderr)
        return 2

    mode = "applied" if args.apply else "dry-run"
    print(f"{mode}: parsed={result['parsed']} global={result['global']} node={result['node']} skipped={result['skipped']}")
    if result["global_keys"]:
        print("global keys: " + ", ".join(result["global_keys"]))
    if result["node_keys"]:
        print("node keys: " + ", ".join(result["node_keys"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
