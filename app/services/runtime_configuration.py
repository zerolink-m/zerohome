"""Seed and apply normalized DB-backed global and node runtime settings."""
from __future__ import annotations

import secrets
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.node import Node
from app.models.runtime_config import GlobalSetting, NodeCapability, NodeSetting, SystemSettings
from app.services.cluster.identity import (
    get_node_id,
    identity_registration_pending,
    mark_identity_registered,
    local_capabilities,
    supported_capabilities,
)
from app.version import APP_VERSION

_BOOTSTRAP_FIELDS = {
    "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME",
    "DB_READ_TIMEOUT_SECONDS", "DB_WRITE_TIMEOUT_SECONDS", "HOST", "PORT",
}
_NODE_FIELDS = {
    "NODE_ADDRESS", "CACHE_DIR", "MOTION_FILES_DIR", "HLS_TEMP_DIR", "INGEST_REGISTRY_DIR",
    "CLUSTER_MEDIA_HOST", "CLUSTER_MEDIA_PORT", "CLUSTER_VIEW_HTTP_PORT", "WEBRTC_PUBLIC_IP",
    "WEBRTC_UDP_PORT_MIN", "WEBRTC_UDP_PORT_MAX", "FFMPEG_BINARY", "FFPROBE_BINARY",
    "INGEST_MAX_BUFFER_BYTES", "INGEST_SNAPSHOT_MAX_PARALLEL", "INGEST_STALL_SECONDS",
    "INGEST_RESTART_MIN_MINUTES", "INGEST_RESTART_MAX_MINUTES", "INGEST_BACKOFF_MAX_SECONDS",
    "INGEST_STOP_TIMEOUT_SECONDS", "INGEST_IDLE_TRANSCODE_LINGER_SECONDS", "INGEST_HUB_WAIT_SECONDS",
    "INGEST_TRANSCODE_STALL_SECONDS", "INGEST_SNAPSHOT_HUB_WAIT_SECONDS", "INGEST_SNAPSHOT_TIMEOUT_SECONDS",
    "INGEST_MOTION_FIRST_FRAME_TIMEOUT_SECONDS", "INGEST_MOTION_STALL_SECONDS",
    "INGEST_MOTION_MAX_FRAME_AGE_SECONDS", "INGEST_MOTION_RESTART_ATTEMPTS", "WEBRTC_MAX_BITRATE",
    "WEBRTC_ICE_TIMEOUT_SECONDS", "WEBRTC_FIRST_FRAME_TIMEOUT_SECONDS", "WEBRTC_STALL_SECONDS",
    "WEBRTC_MAX_LATENCY_MS",
}
_SENSITIVE_NAME_PARTS = ("SECRET", "PASSWORD", "ACCESS_KEY", "PRIVATE_KEY", "TOKEN", "API_KEY")


def _is_secret_setting(name: str) -> bool:
    """Single redaction policy for secret-like settings, including deprecated fields."""
    normalized = name.upper()
    field = settings.model_fields.get(name)
    return any(part in normalized for part in _SENSITIVE_NAME_PARTS) or bool(field and field.repr is False)


def _global_defaults() -> dict:
    data = settings.model_dump()
    return {key: value for key, value in data.items()
            if key not in _BOOTSTRAP_FIELDS and key not in _NODE_FIELDS}


def _node_defaults() -> dict:
    data = settings.model_dump()
    return {key: data[key] for key in _NODE_FIELDS}


async def initialize_runtime_configuration(db: AsyncSession) -> None:
    """Seed missing values, apply persisted values, and register this node."""
    system = await db.get(SystemSettings, 1)
    if system is None:
        system = SystemSettings(id=1, revision=0, updated_at=int(time.time()))
        db.add(system)
        await db.flush()

    global_rows = (await db.execute(select(GlobalSetting))).scalars().all()
    stored = {row.key: row.value for row in global_rows}
    global_by_key = {row.key: row for row in global_rows}
    for key, value in _global_defaults().items():
        if key in ("JWT_SECRET_SESSION", "JWT_SECRET_SHARE") and not str(stored.get(key) or "").strip():
            # Explicit BOOTSTRAP_* values are a one-time import path;
            # otherwise empty legacy rows are repaired with durable keys.
            value = value or secrets.token_urlsafe(48)
            if key in global_by_key:
                global_by_key[key].value = value
            else:
                db.add(GlobalSetting(key=key, value=value))
            stored[key] = value
        elif key not in stored:
            db.add(GlobalSetting(key=key, value=value))
            stored[key] = value

    applied_global = {
        key: value for key, value in stored.items()
        if key in settings.model_fields and key not in _BOOTSTRAP_FIELDS and key not in _NODE_FIELDS
    }
    applied_node = {}
    node_id = get_node_id()
    node = await db.get(Node, node_id)
    if node is None and not identity_registration_pending():
        raise RuntimeError("Local node identity is not registered in the database; refusing startup")

    if node is None:
        node = Node(node_id=node_id, started_at=int(time.time()), heartbeat_at=int(time.time()),
                    capabilities={}, configuration={"identity_id": node_id}, address=None, version=APP_VERSION)
        db.add(node)
        await db.flush()
    else:
        identity_record = dict(node.configuration or {})
        recorded_identity = identity_record.get("identity_id")
        if recorded_identity and recorded_identity != node_id:
            raise RuntimeError("Local node identity does not match the database registration; refusing startup")
        if not recorded_identity:
            identity_record["identity_id"] = node_id
            node.configuration = identity_record

    node_rows = (await db.execute(select(NodeSetting).where(NodeSetting.node_id == node_id))).scalars().all()
    stored_node = {row.key: row.value for row in node_rows}
    for key, value in _node_defaults().items():
        if key not in stored_node:
            db.add(NodeSetting(node_id=node_id, key=key, value=value))
            stored_node[key] = value
    applied_node = {key: value for key, value in stored_node.items() if key in _NODE_FIELDS}

    cap_rows = (await db.execute(select(NodeCapability).where(NodeCapability.node_id == node_id))).scalars().all()
    cap_by_name = {row.capability: row for row in cap_rows}
    for name, supported in supported_capabilities().items():
        row = cap_by_name.get(name)
        if row is None:
            row = NodeCapability(node_id=node_id, capability=name, supported=supported,
                                 enabled=supported, capacity=None)
            db.add(row)
            cap_by_name[name] = row
        else:
            row.supported = supported
    enabled_caps = {name: row.enabled for name, row in cap_by_name.items()}
    now = int(time.time())
    node.started_at = now
    node.heartbeat_at = now
    node.address = str(applied_node.get("NODE_ADDRESS") or "").strip() or None
    node.version = APP_VERSION

    candidate = {**settings.model_dump(), **applied_global, **applied_node}
    validated = type(settings)(_env_file=None, **candidate)
    validated._node_capabilities = enabled_caps
    node.capabilities = local_capabilities(validated)
    await db.commit()
    # If a crash happens before this marker is cleared, the next start can
    # safely repeat registration because the matching node row now exists.
    mark_identity_registered()
    for key, value in validated.model_dump().items():
        if key in settings.model_fields:
            setattr(settings, key, value)
    settings._node_capabilities = enabled_caps


def public_setting_value(key: str, value: object) -> object:
    """Never return credential material through the configuration API."""
    if _is_secret_setting(key):
        return {"configured": bool(str(value or "").strip())}
    return value


def global_fields() -> set[str]:
    return set(_global_defaults())


def node_fields() -> set[str]:
    return set(_NODE_FIELDS)


def secret_fields() -> set[str]:
    return {name for name in settings.model_fields if _is_secret_setting(name)}
