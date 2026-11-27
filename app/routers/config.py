"""Public client configuration and resource endpoints for runtime settings."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import codes
from app.core.config import settings
from app.core.errors import AppError
from app.core.introspection import declare
from app.core.rate_limit import rate_limit
from app.core.response import edited, listing, single, success
from app.core.validation import field_error
from app.deps.auth import TokenPayload, require_access_admin
from app.deps.list_query import ListQuery, list_query
from app.deps.request_body import SingleBody, single_body
from app.db.session import get_db
from app.models.node import Node
from app.models.runtime_config import GlobalSetting, NodeCapability, NodeSetting, SystemSettings
from app.schemas.config import (
    ConfigOut,
    NodeConfigurationPatch,
    NodeConfigurationOut,
    ServerTimeOut,
    SettingOut,
    SettingPatch,
)
from app.services import frontend_version
from app.services.cluster.identity import worker_enabled
from app.services.runtime_configuration import (
    global_fields,
    node_fields,
    public_setting_value,
)
from app.version import APP_VERSION

router = APIRouter(prefix="/v1", tags=["config"])

# Legacy codes stored in frontend_versions.json are translated to cursedapi symbols.
_VERSION_CODES = {
    0: codes.SUCCESS,
    5: codes.FRONTEND_UPDATE_AVAILABLE,
    67: codes.FRONTEND_UPDATE_NEEDED,
}

_setting_query = list_query(
    model=GlobalSetting,
    sortable=("key",),
    searchable=("key",),
)
_node_query = list_query(
    model=Node,
    sortable=("node_id", "heartbeat_at", "version"),
    searchable=("node_id", "address", "version"),
)


def _public_setting(row: GlobalSetting) -> dict:
    return SettingOut(key=row.key, value=public_setting_value(row.key, row.value)).model_dump()


async def _bump_revision(db: AsyncSession) -> int:
    system = await db.scalar(select(SystemSettings).where(SystemSettings.id == 1).with_for_update())
    if system is None:
        system = SystemSettings(id=1, revision=0, updated_at=int(time.time()))
        db.add(system)
        await db.flush()
    system.revision += 1
    system.updated_at = int(time.time())
    return system.revision


async def _node_payload(node: Node, db: AsyncSession) -> dict:
    rows = (await db.execute(
        select(NodeSetting).where(NodeSetting.node_id == node.node_id).order_by(NodeSetting.key)
    )).scalars().all()
    caps = (await db.execute(
        select(NodeCapability).where(NodeCapability.node_id == node.node_id)
        .order_by(NodeCapability.capability)
    )).scalars().all()
    return NodeConfigurationOut.model_validate({
        "node_id": node.node_id,
        "address": node.address,
        "started_at": node.started_at,
        "heartbeat_at": node.heartbeat_at,
        "version": node.version,
        "settings": {row.key: public_setting_value(row.key, row.value) for row in rows},
        "capabilities": {
            row.capability: {
                "supported": row.supported,
                "enabled": row.enabled,
                "capacity": row.capacity,
            }
            for row in caps
        },
    }).model_dump()


@router.get("/config/frontend")
async def get_frontend_config():
    return single("config", ConfigOut(version=APP_VERSION).model_dump(exclude_none=True))


@router.get("/config/time", dependencies=[Depends(rate_limit("config_time", capacity=20, per_minute=120))])
async def get_time():
    """Current server time, unix timestamp UTC (no auth required)."""
    return success(ServerTimeOut(time=int(time.time())).model_dump())


@router.get("/config", dependencies=[Depends(rate_limit("config", capacity=20, per_minute=120))])
async def get_config(v: str | None = Query(None, description="Client version: vMAJOR.MINOR.PATCH-Channel")):
    """Frontend compatibility check; the query key is kept for existing clients."""
    if v is None:
        raise AppError(codes.FRONTEND_UPDATE_NEEDED, "Требуется обновление фронтенда")
    client = frontend_version.parse_client_version(v)
    if client is None:
        raise AppError(
            codes.BAD_REQUEST,
            "Параметр v имеет неверный формат",
            query={"v": field_error("must match vMAJOR.MINOR.PATCH-Release|Beta|Alpha")},
        )
    legacy = frontend_version.resolve_version_code(client)
    code = codes.FRONTEND_UPDATE_NEEDED if legacy is None else _VERSION_CODES.get(legacy, codes.SUCCESS)
    if code == codes.FRONTEND_UPDATE_NEEDED:
        raise AppError(codes.FRONTEND_UPDATE_NEEDED, "Требуется обновление фронтенда")
    return success({"config": ConfigOut(version=v, actual_version=APP_VERSION).model_dump()}, code=code)


declare(get_config, "x-client-version")


@router.get("/config/frontend/supported_streams")
async def get_supported_streams():
    """Public list of stream formats currently advertised by this node."""
    streams = [{
        "format": "fmp4",
        "enabled": True,
        "readonly": True,
        "name": "Fragmented MP4",
        "description": "Low latency streaming via HTTP",
        "latency": "2-5s",
        "endpoint": "/v1/cams/{id}/stream",
        "mime_type": "video/mp4",
        "features": ["low_latency", "seek", "pause"],
    }]

    if worker_enabled(settings, "hls"):
        streams.append({
            "format": "hls",
            "enabled": True,
            "readonly": False,
            "name": "HLS (HTTP Live Streaming)",
            "description": "Adaptive streaming for wide compatibility",
            "latency": "6-30s",
            "endpoint": "/v1/cams/{id}/hls/playlist.m3u8",
            "mime_type": "application/vnd.apple.mpegurl",
            "features": ["adaptive_bitrate", "wide_compatibility", "ios_native"],
        })

    if worker_enabled(settings, "webrtc"):
        streams.append({
            "format": "webrtc",
            "enabled": True,
            "readonly": False,
            "name": "WebRTC",
            "description": "Ultra low latency P2P streaming",
            "latency": "<1s",
            "endpoint": "/v1/cams/{id}/webrtc",
            "protocol": "websocket",
            "features": ["ultra_low_latency", "p2p"],
        })

    return success({"streams": streams})


@router.get("/system-settings", dependencies=[Depends(rate_limit("system_settings_read", capacity=60, per_minute=60))])
async def list_system_settings(
    query: ListQuery = Depends(_setting_query),
    _: TokenPayload = Depends(require_access_admin),
    db: AsyncSession = Depends(get_db),
):
    rows, total = await query.paginate(db, select(GlobalSetting))
    system = await db.get(SystemSettings, 1)
    return listing(
        "system_settings",
        [_public_setting(row) for row in rows],
        query=query,
        total=total,
        metadata={"revision": system.revision if system else 0},
    )


@router.get("/system-settings/{key}", dependencies=[Depends(rate_limit("system_settings_read", capacity=60, per_minute=60))])
async def get_system_setting(
    key: str,
    _: TokenPayload = Depends(require_access_admin),
    db: AsyncSession = Depends(get_db),
):
    if key not in global_fields():
        raise AppError(codes.OBJECT_NOT_FOUND, "Setting not found")
    row = await db.get(GlobalSetting, key)
    if row is None:
        raise AppError(codes.OBJECT_NOT_FOUND, "Setting not found")
    system = await db.get(SystemSettings, 1)
    return single("system_setting", _public_setting(row), metadata={"revision": system.revision if system else 0})


@router.patch("/system-settings/{key}", dependencies=[Depends(rate_limit("system_settings_write", capacity=30, per_minute=30))])
async def update_system_setting(
    key: str,
    body: SingleBody[SettingPatch] = Depends(single_body("setting", SettingPatch)),
    _: TokenPayload = Depends(require_access_admin),
    db: AsyncSession = Depends(get_db),
):
    if key not in global_fields():
        raise AppError(codes.OBJECT_NOT_FOUND, "Setting not found")
    current = {
        row.key: row.value
        for row in (await db.execute(select(GlobalSetting))).scalars().all()
    }
    candidate = {**settings.model_dump(), **current, key: body.obj.value}
    try:
        validated = type(settings)(_env_file=None, **candidate)
    except (ValidationError, ValueError) as exc:
        raise AppError(codes.VALIDATION_ERROR, "Invalid setting value", fields={
            "value": field_error("does not match the setting type or constraints")
        }) from exc

    row = await db.get(GlobalSetting, key)
    if row is None:
        row = GlobalSetting(key=key, value=getattr(validated, key))
        db.add(row)
    else:
        row.value = getattr(validated, key)
    revision = await _bump_revision(db)
    await db.commit()
    return edited(
        "system_setting",
        _public_setting(row),
        metadata={"revision": revision, "restart_required": True},
    )


@router.get("/nodes", dependencies=[Depends(rate_limit("nodes_read", capacity=60, per_minute=60))])
async def list_nodes(
    query: ListQuery = Depends(_node_query),
    _: TokenPayload = Depends(require_access_admin),
    db: AsyncSession = Depends(get_db),
):
    rows, total = await query.paginate(db, select(Node))
    values = [await _node_payload(row, db) for row in rows]
    return listing("nodes", values, query=query, total=total)


@router.get("/nodes/{node_id}", dependencies=[Depends(rate_limit("nodes_read", capacity=60, per_minute=60))])
async def get_node(
    node_id: str,
    _: TokenPayload = Depends(require_access_admin),
    db: AsyncSession = Depends(get_db),
):
    node = await db.get(Node, node_id)
    if node is None:
        raise AppError(codes.OBJECT_NOT_FOUND, "Node not found")
    return single("node", await _node_payload(node, db))


@router.patch("/nodes/{node_id}", dependencies=[Depends(rate_limit("nodes_write", capacity=30, per_minute=30))])
async def update_node(
    node_id: str,
    body: SingleBody[NodeConfigurationPatch] = Depends(single_body("node", NodeConfigurationPatch)),
    _: TokenPayload = Depends(require_access_admin),
    db: AsyncSession = Depends(get_db),
):
    node = await db.get(Node, node_id)
    if node is None:
        raise AppError(codes.OBJECT_NOT_FOUND, "Node not found")

    setting_values = body.obj.settings
    capability_values = body.obj.capabilities
    unknown = sorted(set(setting_values) - node_fields())
    if unknown:
        raise AppError(codes.UNKNOWN_FIELD, "Unknown or non-node setting", fields={
            "settings": field_error("contains unknown or non-node keys", value=unknown)
        })
    rows = (await db.execute(select(NodeSetting).where(NodeSetting.node_id == node_id))).scalars().all()
    current = {row.key: row.value for row in rows}
    candidate = {**settings.model_dump(), **current, **setting_values}
    try:
        validated = type(settings)(_env_file=None, **candidate)
    except (ValidationError, ValueError) as exc:
        raise AppError(codes.VALIDATION_ERROR, "Invalid node configuration") from exc

    caps = (await db.execute(select(NodeCapability).where(NodeCapability.node_id == node_id))).scalars().all()
    cap_map = {row.capability: row for row in caps}
    bad_caps = sorted(set(capability_values) - set(cap_map))
    if bad_caps:
        raise AppError(codes.UNKNOWN_FIELD, "Unknown node capability", fields={
            "capabilities": field_error("contains unknown capabilities", value=bad_caps)
        })

    for key in setting_values:
        row = next((item for item in rows if item.key == key), None)
        if row is None:
            db.add(NodeSetting(node_id=node_id, key=key, value=getattr(validated, key)))
        else:
            row.value = getattr(validated, key)
    for name, policy in capability_values.items():
        if not isinstance(policy, dict) or set(policy) - {"enabled", "capacity"}:
            raise AppError(codes.VALIDATION_ERROR, f"Invalid capability policy: {name}")
        row = cap_map[name]
        if "enabled" in policy:
            if not isinstance(policy["enabled"], bool) or (policy["enabled"] and not row.supported):
                raise AppError(codes.VALIDATION_ERROR, f"Capability is not supported or enabled is invalid: {name}")
            row.enabled = policy["enabled"]
        if "capacity" in policy:
            capacity = policy["capacity"]
            if capacity is not None and (not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 0):
                raise AppError(codes.VALIDATION_ERROR, f"Invalid capability capacity: {name}")
            row.capacity = capacity

    revision = await _bump_revision(db)
    await db.commit()
    await db.refresh(node)
    return edited(
        "node",
        await _node_payload(node, db),
        metadata={"revision": revision, "restart_required": True},
    )
