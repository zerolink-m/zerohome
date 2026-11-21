from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.node import Node
from app.models.runtime_config import GlobalSetting, NodeSetting, SystemSettings
from app.services.cluster import identity
from deploy import import_legacy_settings as importer


async def _database(tmp_path, monkeypatch, *, register_node=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-import.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    node_id = "test-import-node"
    monkeypatch.setattr(identity, "IDENTITY_FILE", tmp_path / "identity" / "node-id")
    monkeypatch.setattr(identity, "_generated_id", node_id)
    if register_node:
        async with sessions() as db:
            db.add(Node(node_id=node_id, started_at=1, heartbeat_at=1, capabilities={},
                        configuration={"identity_id": node_id}, address=None, version="test"))
            db.add(SystemSettings(id=1, revision=0, updated_at=1))
            await db.commit()
    else:
        async with sessions() as db:
            db.add(SystemSettings(id=1, revision=0, updated_at=1))
            await db.commit()
    return engine, sessions, node_id


def _write_source(tmp_path, content: str):
    path = tmp_path / "old-runtime.conf"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_dry_run_does_not_write_anything(tmp_path, monkeypatch):
    engine, sessions, node_id = await _database(tmp_path, monkeypatch)
    source = _write_source(tmp_path, "HLS_ENABLED=true\nCACHE_DIR=/legacy/cache\n")
    async with sessions() as db:
        result = await importer.import_legacy_settings(db, source, apply=False, node_id=node_id)
    async with sessions() as db:
        assert (await db.get(SystemSettings, 1)).revision == 0
        assert (await db.execute(select(GlobalSetting))).scalars().all() == []
        assert (await db.execute(select(NodeSetting))).scalars().all() == []
    assert (result["global"], result["node"], result["skipped"]) == (1, 1, 0)
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_imports_global_and_node_settings_and_refuses_repeat(tmp_path, monkeypatch):
    engine, sessions, node_id = await _database(tmp_path, monkeypatch)
    source = _write_source(tmp_path, "HLS_ENABLED=true\nCACHE_DIR=/legacy/cache\n")
    async with sessions() as db:
        result = await importer.import_legacy_settings(db, source, apply=True, node_id=node_id)
    async with sessions() as db:
        assert (await db.get(GlobalSetting, "HLS_ENABLED")).value is True
        assert (await db.get(NodeSetting, (node_id, "CACHE_DIR"))).value == "/legacy/cache"
        assert (await db.get(SystemSettings, 1)).revision == 1
    async with sessions() as db:
        with pytest.raises(importer.ImportFailure, match="revision is not 0"):
            await importer.import_legacy_settings(db, source, apply=True, node_id=node_id)
    assert result["global_keys"] == ["HLS_ENABLED"]
    assert result["node_keys"] == ["CACHE_DIR"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_bootstrap_and_listener_fields_are_skipped(tmp_path, monkeypatch):
    engine, sessions, node_id = await _database(tmp_path, monkeypatch)
    source = _write_source(
        tmp_path,
        "HLS_ENABLED=true\nDB_PASSWORD=must-never-import\nDB_HOST=db.example\nHOST=0.0.0.0\nPORT=9000\nNOT_A_SETTING=value\n",
    )
    async with sessions() as db:
        result = await importer.import_legacy_settings(db, source, apply=True, node_id=node_id)
    async with sessions() as db:
        assert [row.key for row in (await db.execute(select(GlobalSetting))).scalars().all()] == ["HLS_ENABLED"]
        assert await db.get(GlobalSetting, "DB_PASSWORD") is None
        assert await db.get(GlobalSetting, "DB_HOST") is None
        assert await db.get(NodeSetting, (node_id, "HOST")) is None
        assert await db.get(NodeSetting, (node_id, "PORT")) is None
    assert result["global"] == 1 and result["node"] == 0 and result["skipped"] == 5
    await engine.dispose()


def test_secret_values_never_appear_in_command_output(tmp_path, monkeypatch, capsys):
    secret = "sentinel-secret-value-do-not-print"
    source = _write_source(tmp_path, f"JWT_SECRET_SHARE={secret}\nHLS_ENABLED=true\n")

    async def run(_source, apply):
        return {
            "parsed": 2,
            "global": 2,
            "node": 0,
            "skipped": 0,
            "global_keys": ["JWT_SECRET_SHARE", "HLS_ENABLED"],
            "node_keys": [],
        }

    monkeypatch.setattr(importer, "_run_cli", run)
    monkeypatch.setattr(importer.sys, "argv", ["import_legacy_settings.py", "--source", str(source)])
    assert importer.main() == 0
    output = capsys.readouterr()
    assert secret not in output.out and secret not in output.err
    assert "JWT_SECRET_SHARE" in output.out


@pytest.mark.asyncio
async def test_invalid_data_and_unregistered_node_fail_without_partial_writes(tmp_path, monkeypatch):
    engine, sessions, node_id = await _database(tmp_path, monkeypatch)
    secret = "invalid-secret-must-not-leak"
    source = _write_source(tmp_path, f"HLS_ENABLED={secret}\n")
    async with sessions() as db:
        with pytest.raises(importer.ImportFailure) as error:
            await importer.import_legacy_settings(db, source, apply=True, node_id=node_id)
    assert secret not in str(error.value)
    async with sessions() as db:
        assert (await db.get(SystemSettings, 1)).revision == 0
        assert (await db.execute(select(GlobalSetting))).scalars().all() == []

    other_engine, other_sessions, other_node = await _database(
        tmp_path / "unregistered", monkeypatch, register_node=False,
    )
    safe_source = _write_source(tmp_path, "HLS_ENABLED=true\n")
    async with other_sessions() as db:
        with pytest.raises(importer.ImportFailure, match="not registered"):
            await importer.import_legacy_settings(db, safe_source, apply=True, node_id=other_node)
    async with other_sessions() as db:
        assert (await db.get(SystemSettings, 1)).revision == 0
        assert (await db.execute(select(GlobalSetting))).scalars().all() == []
    await engine.dispose()
    await other_engine.dispose()
