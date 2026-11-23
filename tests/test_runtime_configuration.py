from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.core.config import Settings
from app.models.node import Node
from app.models.runtime_config import GlobalSetting, NodeCapability, NodeSetting, SystemSettings
from app.services import runtime_configuration as runtime
from app.services.cluster import identity
from app.version import APP_VERSION


def test_runtime_environment_is_limited_to_database_bootstrap(monkeypatch):
    # The suite-wide credentials are an explicit bootstrap fixture. This test
    # starts without that one-time import to prove ordinary env secrets are ignored.
    monkeypatch.delenv("BOOTSTRAP_JWT_SECRET_SESSION", raising=False)
    monkeypatch.delenv("BOOTSTRAP_JWT_SECRET_SHARE", raising=False)
    monkeypatch.setenv("DB_HOST", "database.internal")
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setenv("HLS_ENABLED", "true")
    monkeypatch.setenv("BOOTSTRAP_HLS_ENABLED", "true")
    monkeypatch.setenv("NODE_ID", "environment-node")
    monkeypatch.setenv("JWT_SECRET_SESSION", "legacy-env-secret")
    monkeypatch.setenv("BOOTSTRAP_NODE_CAPABILITIES", '{"ingest": false}')
    config = Settings(_env_file=None)
    assert config.DB_HOST == "database.internal"
    assert config.HOST == "127.0.0.1"
    assert config.PORT == 8123
    assert config.HLS_ENABLED is False
    assert not hasattr(config, "NODE_ID")
    assert config._node_capabilities == {}
    assert config.JWT_SECRET_SESSION == ""
    monkeypatch.setenv("BOOTSTRAP_JWT_SECRET_SESSION", "explicit-import-secret")
    assert Settings(_env_file=None).JWT_SECRET_SESSION == "explicit-import-secret"
    assert "VERSION" not in Settings.model_fields
    assert APP_VERSION == "4.8.2"


def test_local_capability_and_global_switch_both_gate_workers():
    config = Settings(_env_file=None)
    config._node_capabilities = {"healthcheck": False, "motion": True, "ingest": False}
    assert identity.worker_enabled(config, "healthcheck") is False
    assert identity.worker_enabled(config, "motion") is True
    assert identity.worker_enabled(config, "ingest") is False
    config.MOTION_DETECTION_ENABLED = False
    config._node_capabilities["motion"] = True
    assert identity.worker_enabled(config, "motion") is False


@pytest.mark.asyncio
async def test_version_routes_use_app_version_constant(tmp_path, monkeypatch):
    from app.routers.config import get_config, get_frontend_config
    from app.routers.service import version
    from app.services import frontend_version

    versions_path = tmp_path / "frontend_versions.json"
    versions_path.write_text(json.dumps({"versions": {"v*.*.*-*": 0}}))
    monkeypatch.setattr(frontend_version, "_JSON_PATH", versions_path)
    frontend_version.reset_cache_for_tests()

    service_response = json.loads((await version()).body)
    frontend_response = json.loads((await get_frontend_config()).body)
    client_response = json.loads((await get_config("v4.7.0-Release")).body)
    assert service_response["data"]["version"]["app"] == APP_VERSION
    assert frontend_response["data"]["config"]["version"] == APP_VERSION
    assert client_response["data"]["config"]["actual_version"] == APP_VERSION
    frontend_version.reset_cache_for_tests()


def test_every_secret_like_setting_is_redacted():
    from app.services.runtime_configuration import public_setting_value, secret_fields

    expected = {
        name for name, field in Settings.model_fields.items()
        if any(marker in name.upper() for marker in
               ("SECRET", "PASSWORD", "ACCESS_KEY", "PRIVATE_KEY", "TOKEN", "API_KEY"))
        or field.repr is False
    }
    assert expected <= secret_fields()
    assert {"JWT_SECRET_ACCESS", "JWT_SECRET_ACCESS_ADMIN"} <= secret_fields()
    for name in secret_fields():
        assert public_setting_value(name, "must-not-be-shown") == {"configured": True}
        assert public_setting_value(name, "") == {"configured": False}
    assert public_setting_value("legacy_unknown_secret", "redaction-test") == {"configured": True}


@pytest.mark.asyncio
async def test_runtime_configuration_api_redacts_all_secret_settings():
    from app.core.security import TokenPayload
    from app.routers.config import get_system_setting

    exposed_secrets = runtime.secret_fields() & runtime.global_fields()
    assert exposed_secrets
    values = {name: f"redaction-test-{index}" for index, name in enumerate(exposed_secrets)}
    rows = {name: GlobalSetting(key=name, value=value) for name, value in values.items()}

    class _Database:
        async def get(self, model, key):
            if model is GlobalSetting:
                return rows.get(key)
            return SystemSettings(id=1, revision=1, updated_at=1)

    admin = TokenPayload(kind="bearer", uid=1, sid=1, role="admin", iat=1, exp=2)
    db = _Database()
    for name, secret in values.items():
        response = await get_system_setting(name, admin, db)
        body = response.body.decode()
        public = json.loads(body)["data"]["system_setting"]
        assert public["value"] == {"configured": True}
        assert secret not in body


@pytest.mark.asyncio
async def test_runtime_configuration_seeds_then_applies_database_values(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(identity, "IDENTITY_FILE", tmp_path / "identity" / "node-id")
    monkeypatch.setattr(identity, "_generated_id", None)
    monkeypatch.setattr(
        runtime, "settings",
        Settings(_env_file=None, JWT_SECRET_SESSION="test-session-key", JWT_SECRET_SHARE="test-share-key"),
    )

    async with sessions() as db:
        await runtime.initialize_runtime_configuration(db)
        node = (await db.execute(select(Node))).scalar_one()
        assert node.node_id == json.loads(identity.IDENTITY_FILE.read_text())["node_id"]
        assert node.configuration["identity_id"] == node.node_id
        assert await db.get(NodeCapability, (node.node_id, "ingest")) is not None
        setting = await db.get(GlobalSetting, "SESSION_CLEANUP_ENABLED")
        assert setting.value is True
        setting.value = False
        node_setting = await db.get(NodeSetting, (node.node_id, "NODE_ADDRESS"))
        node_setting.value = "node.example:4700"
        capability = await db.get(NodeCapability, (node.node_id, "healthcheck"))
        capability.enabled = False
        await db.commit()

    async with sessions() as db:
        await runtime.initialize_runtime_configuration(db)
        node = (await db.execute(select(Node))).scalar_one()
        assert runtime.settings.SESSION_CLEANUP_ENABLED is False
        assert runtime.settings.NODE_ADDRESS == "node.example:4700"
        assert node.capabilities["healthcheck"] is False
        assert await db.get(SystemSettings, 1) is not None

    await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_seeds_durable_secrets_and_recovers_pending_identity(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'secrets.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    identity_path = tmp_path / "identity" / "node-id"
    monkeypatch.setattr(identity, "IDENTITY_FILE", identity_path)
    monkeypatch.setattr(identity, "_generated_id", None)
    identity_path.parent.mkdir(parents=True)
    identity_path.write_text(json.dumps({"node_id": "crash-recovery-node", "state": "pending"}))
    monkeypatch.delenv("BOOTSTRAP_JWT_SECRET_SESSION", raising=False)
    monkeypatch.delenv("BOOTSTRAP_JWT_SECRET_SHARE", raising=False)
    monkeypatch.setattr(runtime, "settings", Settings(_env_file=None))
    async with sessions() as db:
        await runtime.initialize_runtime_configuration(db)
        session_key = await db.get(GlobalSetting, "JWT_SECRET_SESSION")
        share_key = await db.get(GlobalSetting, "JWT_SECRET_SHARE")
        assert len(session_key.value) >= 64
        assert len(share_key.value) >= 64
    assert not identity.identity_registration_pending()
    assert identity.get_node_id() == "crash-recovery-node"
    await engine.dispose()
