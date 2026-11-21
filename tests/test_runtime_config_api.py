from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request

from app.core.codes import FORBIDDEN_ROLE
from app.core.errors import AppError, app_error_handler
from app.db.session import get_db
from app.deps.auth import require_access_admin
from app.models.node import Node
from app.models.runtime_config import GlobalSetting, NodeCapability, NodeSetting, SystemSettings
from app.routers.config import router


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _MemoryDB:
    """Small DB double so these HTTP tests do not initialize SQLite or services."""

    def __init__(self):
        self.system = SystemSettings(id=1, revision=3, updated_at=1)
        self.globals = {"JWT_SECRET_SHARE": GlobalSetting(key="JWT_SECRET_SHARE", value="do-not-return-this")}
        self.node = Node(node_id="api-test-node", started_at=1, heartbeat_at=1,
                         capabilities={}, configuration={}, version="test")
        self.node_settings = {"CACHE_DIR": NodeSetting(node_id=self.node.node_id,
                                                          key="CACHE_DIR", value="/cache")}
        self.capabilities = {"motion": NodeCapability(node_id=self.node.node_id,
                                                         capability="motion", supported=True,
                                                         enabled=True, capacity=1)}

    async def get(self, model, key):
        if model is SystemSettings:
            return self.system
        if model is Node:
            return self.node if key == self.node.node_id else None
        if model is GlobalSetting:
            return self.globals.get(key)
        if model is NodeSetting:
            return self.node_settings.get(key[1])
        if model is NodeCapability:
            return self.capabilities.get(key[1])
        return None

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        rows = {
            GlobalSetting: self.globals.values(),
            NodeSetting: self.node_settings.values(),
            NodeCapability: self.capabilities.values(),
        }.get(entity, ())
        return _Result(rows)

    async def scalar(self, _statement):
        return self.system

    def add(self, row):
        if isinstance(row, GlobalSetting):
            self.globals[row.key] = row
        elif isinstance(row, NodeSetting):
            self.node_settings[row.key] = row
        elif isinstance(row, NodeCapability):
            self.capabilities[row.capability] = row

    async def flush(self):
        return None

    async def commit(self):
        return None


@pytest.fixture
async def runtime_client():
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(AppError, app_error_handler)
    db = _MemoryDB()

    async def authorize(request: Request):
        if request.headers.get("x-test-role") != "admin":
            raise AppError(FORBIDDEN_ROLE, "Admin role required")
        return None

    async def provide_db():
        yield db

    app.dependency_overrides[require_access_admin] = authorize
    app.dependency_overrides[get_db] = provide_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, db


@pytest.mark.asyncio
async def test_global_runtime_get_redacts_secrets_and_patch_requires_cursedapi_envelope(runtime_client):
    client, db = runtime_client
    admin = {"x-test-role": "admin"}
    got = await client.get("/v1/config/runtime", headers=admin)
    assert got.status_code == 200
    assert got.json()["data"]["revision"] == 3
    assert got.json()["data"]["settings"]["JWT_SECRET_SHARE"] == {"configured": True}
    assert "do-not-return-this" not in got.text
    assert (await client.get("/v1/config/runtime")).json()["code"] == "050"

    raw = await client.patch("/v1/config/runtime", headers=admin,
                             json={"MOTION_DETECTION_ENABLED": False})
    assert raw.json()["code"] == "109"

    patched = await client.patch(
        "/v1/config/runtime", headers=admin,
        json={"data": {"runtime": {"settings": {"MOTION_DETECTION_ENABLED": False}}},
              "options": {"partial_allowed": False}},
    )
    assert patched.status_code == 200
    assert patched.json()["data"] == {"revision": 4, "restart_required": True}
    assert db.globals["MOTION_DETECTION_ENABLED"].value is False


@pytest.mark.asyncio
async def test_node_runtime_get_and_patch_use_cursedapi_envelope(runtime_client):
    client, db = runtime_client
    headers = {"x-test-role": "admin"}
    path = "/v1/config/runtime/nodes/api-test-node"
    got = await client.get(path, headers=headers)
    assert got.status_code == 200
    assert got.json()["data"]["settings"]["CACHE_DIR"] == "/cache"
    assert got.json()["data"]["capabilities"]["motion"]["enabled"] is True

    raw = await client.patch(path, headers=headers,
                             json={"capabilities": {"motion": {"enabled": False}}})
    assert raw.json()["code"] == "109"

    patched = await client.patch(
        path, headers=headers,
        json={"data": {"runtime": {
            "settings": {"CACHE_DIR": "/srv/cache"},
            "capabilities": {"motion": {"enabled": False, "capacity": 0}},
        }}},
    )
    assert patched.status_code == 200
    assert patched.json()["data"] == {"revision": 4, "restart_required": True}
    assert db.node_settings["CACHE_DIR"].value == "/srv/cache"
    assert db.capabilities["motion"].enabled is False
    assert db.capabilities["motion"].capacity == 0
