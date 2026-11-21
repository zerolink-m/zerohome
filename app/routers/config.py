"""Config (virtual object): frontend-version check, supported streams and server time."""
import time

from fastapi import APIRouter, Depends, Query
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import (
    BAD_REQUEST,
    FRONTEND_UPDATE_AVAILABLE,
    FRONTEND_UPDATE_NEEDED,
    SUCCESS,
)
from app.core.config import settings
from app.core.errors import AppError
from app.core.rate_limit import rate_limit
from app.core.response import success
from app.core.validation import field_error
from app.deps.request_body import SingleBody, single_body
from app.deps.auth import TokenPayload, require_access_admin
from app.db.session import get_db
from app.models.node import Node
from app.models.runtime_config import GlobalSetting, NodeCapability, NodeSetting, SystemSettings
from app.schemas.config import ConfigOut, RuntimeGlobalPatch, RuntimeNodePatch, ServerTimeOut
from app.services import frontend_version
from app.services.runtime_configuration import (
    global_fields, node_fields, public_setting_value, secret_fields,
)
from app.services.cluster.identity import stream_advertised
from app.version import APP_VERSION

router = APIRouter(prefix="/v1/config", tags=["config"])

# legacy codes stored in frontend_versions.json -> CursedJSON codes
_VERSION_CODES = {0: SUCCESS, 5: FRONTEND_UPDATE_AVAILABLE, 67: FRONTEND_UPDATE_NEEDED}


@router.get("/frontend")
async def get_frontend_config():
    return success({"config": ConfigOut(version=APP_VERSION).model_dump(exclude_none=True)})


@router.get("/time", dependencies=[Depends(rate_limit("config_time", capacity=20, per_minute=120))])
async def get_time():
    """Current server time, unix timestamp UTC (no auth required)."""
    return success(ServerTimeOut(time=int(time.time())).model_dump())


@router.get("", dependencies=[Depends(rate_limit("config", capacity=20, per_minute=120))])
async def get_config(v: str | None = Query(None, description="Client version: vMAJOR.MINOR.PATCH-Channel")):
    """Frontend version check. 000 up to date, 020 update available (data returned),
    063 update required (no data), 040 malformed v."""
    if v is None:
        raise AppError(FRONTEND_UPDATE_NEEDED, "Требуется обновление фронтенда")
    client = frontend_version.parse_client_version(v)
    if client is None:
        raise AppError(
            BAD_REQUEST,
            "Параметр v имеет неверный формат",
            query={"v": field_error("must match vMAJOR.MINOR.PATCH-Release|Beta|Alpha")},
        )
    legacy = frontend_version.resolve_version_code(client)
    code = FRONTEND_UPDATE_NEEDED if legacy is None else _VERSION_CODES.get(legacy, SUCCESS)
    if code == FRONTEND_UPDATE_NEEDED:
        raise AppError(FRONTEND_UPDATE_NEEDED, "Требуется обновление фронтенда")
    return success(
        {"config": ConfigOut(version=v, actual_version=APP_VERSION).model_dump()},
        code=code,
    )


@router.get("/frontend/supported_streams")
async def get_supported_streams():
    """Get list of supported streaming formats (no auth required)"""
    streams = [
        {
            "format": "fmp4",
            "enabled": True,
            "readonly": True,
            "name": "Fragmented MP4",
            "description": "Low latency streaming via HTTP",
            "latency": "2-5s",
            "endpoint": "/v1/cams/{id}/stream",
            "mime_type": "video/mp4",
            "features": ["low_latency", "seek", "pause"]
        }
    ]

    if stream_advertised(settings, "hls"):
        streams.append({
            "format": "hls",
            "enabled": True,
            "readonly": False,
            "name": "HLS (HTTP Live Streaming)",
            "description": "Adaptive streaming for wide compatibility",
            "latency": "6-30s",
            "endpoint": "/v1/cams/{id}/hls/playlist.m3u8",
            "mime_type": "application/vnd.apple.mpegurl",
            "features": ["adaptive_bitrate", "wide_compatibility", "ios_native"]
        })

    if stream_advertised(settings, "webrtc"):
        # Check if STUN server is configured
        if settings.WEBRTC_STUN_SERVER:
            streams.append({
                "format": "webrtc",
                "enabled": True,
                "readonly": False,
                "name": "WebRTC",
                "description": "Ultra low latency P2P streaming",
                "latency": "<1s",
                "endpoint": "/v1/cams/{id}/webrtc",
                "protocol": "websocket",
                "features": ["ultra_low_latency", "p2p"]
            })

    return success({"streams": streams})


@router.get("/runtime")
async def get_runtime_configuration(
    _: TokenPayload = Depends(require_access_admin), db: AsyncSession = Depends(get_db),
):
    """Read DB-backed configuration; credential values are always redacted."""
    system = await db.get(SystemSettings, 1)
    rows = (await db.execute(select(GlobalSetting).order_by(GlobalSetting.key))).scalars().all()
    return success({
        "revision": system.revision if system else 0,
        "settings": {row.key: public_setting_value(row.key, row.value) for row in rows},
        "secret_fields": sorted(secret_fields()),
    })


@router.patch("/runtime")
async def update_runtime_configuration(
    body: SingleBody[RuntimeGlobalPatch] = Depends(single_body("runtime", RuntimeGlobalPatch)),
    _: TokenPayload = Depends(require_access_admin), db: AsyncSession = Depends(get_db),
):
    """Persist shared feature and integration settings for the next restart."""
    values = body.obj.settings
    allowed = global_fields()
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise AppError(BAD_REQUEST, "Unknown or non-global setting", query={"keys": unknown})
    current = {row.key: row.value for row in
               (await db.execute(select(GlobalSetting))).scalars().all()}
    candidate = {**settings.model_dump(), **current, **values}
    try:
        validated = type(settings)(_env_file=None, **candidate)
    except (ValidationError, ValueError) as exc:
        raise AppError(BAD_REQUEST, "Invalid runtime configuration") from exc
    system = await db.scalar(select(SystemSettings).where(SystemSettings.id == 1).with_for_update())
    if system is None:
        system = SystemSettings(id=1, revision=0, updated_at=int(time.time()))
        db.add(system)
        await db.flush()
    for key, value in values.items():
        row = await db.get(GlobalSetting, key)
        if row is None:
            db.add(GlobalSetting(key=key, value=getattr(validated, key)))
        else:
            row.value = getattr(validated, key)
    system.revision += 1
    system.updated_at = int(time.time())
    await db.commit()
    return success({"revision": system.revision, "restart_required": True})


@router.get("/runtime/nodes/{node_id}")
async def get_node_runtime_configuration(
    node_id: str, _: TokenPayload = Depends(require_access_admin), db: AsyncSession = Depends(get_db),
):
    node = await db.get(Node, node_id)
    if node is None:
        raise AppError(BAD_REQUEST, "Unknown node")
    rows = (await db.execute(select(NodeSetting).where(NodeSetting.node_id == node_id)
                             .order_by(NodeSetting.key))).scalars().all()
    caps = (await db.execute(select(NodeCapability).where(NodeCapability.node_id == node_id)
                             .order_by(NodeCapability.capability))).scalars().all()
    return success({
        "node": {"node_id": node.node_id, "address": node.address, "heartbeat_at": node.heartbeat_at,
                 "version": node.version},
        "settings": {row.key: public_setting_value(row.key, row.value) for row in rows},
        "capabilities": {row.capability: {"supported": row.supported, "enabled": row.enabled,
                                            "capacity": row.capacity} for row in caps},
    })


@router.patch("/runtime/nodes/{node_id}")
async def update_node_runtime_configuration(
    node_id: str,
    body: SingleBody[RuntimeNodePatch] = Depends(single_body("runtime", RuntimeNodePatch)),
    _: TokenPayload = Depends(require_access_admin), db: AsyncSession = Depends(get_db),
):
    """Persist node-local settings and capability policy for its next restart."""
    node = await db.get(Node, node_id)
    if node is None:
        raise AppError(BAD_REQUEST, "Unknown node")
    setting_values = body.obj.settings
    capability_values = body.obj.capabilities
    unknown = sorted(set(setting_values) - node_fields())
    if unknown:
        raise AppError(BAD_REQUEST, "Unknown or non-node setting", query={"keys": unknown})
    rows = (await db.execute(select(NodeSetting).where(NodeSetting.node_id == node_id))).scalars().all()
    current = {row.key: row.value for row in rows}
    candidate = {**settings.model_dump(), **current, **setting_values}
    try:
        validated = type(settings)(_env_file=None, **candidate)
    except (ValidationError, ValueError) as exc:
        raise AppError(BAD_REQUEST, "Invalid node configuration") from exc
    caps = (await db.execute(select(NodeCapability).where(NodeCapability.node_id == node_id))).scalars().all()
    cap_map = {row.capability: row for row in caps}
    bad_caps = sorted(set(capability_values) - set(cap_map))
    if bad_caps:
        raise AppError(BAD_REQUEST, "Unknown node capability", query={"capabilities": bad_caps})
    for key, value in setting_values.items():
        row = next((item for item in rows if item.key == key), None)
        if row is None:
            db.add(NodeSetting(node_id=node_id, key=key, value=getattr(validated, key)))
        else:
            row.value = getattr(validated, key)
    for name, policy in capability_values.items():
        if not isinstance(policy, dict) or set(policy) - {"enabled", "capacity"}:
            raise AppError(BAD_REQUEST, f"Invalid capability policy: {name}")
        row = cap_map[name]
        if "enabled" in policy:
            if not isinstance(policy["enabled"], bool) or (policy["enabled"] and not row.supported):
                raise AppError(BAD_REQUEST, f"Capability is not supported or enabled is invalid: {name}")
            row.enabled = policy["enabled"]
        if "capacity" in policy:
            capacity = policy["capacity"]
            if capacity is not None and (not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 0):
                raise AppError(BAD_REQUEST, f"Invalid capability capacity: {name}")
            row.capacity = capacity
    system = await db.scalar(select(SystemSettings).where(SystemSettings.id == 1).with_for_update())
    if system is None:
        system = SystemSettings(id=1, revision=0, updated_at=int(time.time()))
        db.add(system)
        await db.flush()
    system.revision += 1
    system.updated_at = int(time.time())
    await db.commit()
    return success({"revision": system.revision, "restart_required": True})
