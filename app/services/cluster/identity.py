"""Node identity and local capabilities."""
from __future__ import annotations

import re
import secrets
import socket
from typing import Any

_generated_id: str | None = None


def get_node_id(settings: Any = None) -> str:
    """NODE_ID if set, else hostname + short random suffix (generated once per process)."""
    global _generated_id
    if settings is None:
        from app.core.config import settings as _settings
        settings = _settings
    configured = (settings.NODE_ID or "").strip()
    if configured:
        return configured[:64]
    if _generated_id is None:
        host = socket.gethostname() or "node"
        _generated_id = f"{host[:55]}-{secrets.token_hex(3)}"
    return _generated_id


def safe_node_dir_name(node_id: str) -> str:
    """Filesystem-safe form of a node id: only [A-Za-z0-9._-], never empty, never '.'/'..'."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", node_id or "")
    name = name.replace("..", "__").strip(".") or "node"
    return name[:64]


def local_capabilities(settings: Any) -> dict[str, bool]:
    """Map existing worker flags to lease function names."""
    return {
        "session_cleanup": bool(settings.SESSION_CLEANUP_ENABLED),
        "healthcheck": bool(settings.HEALTHCHECK_ENABLED),
        "switch_sync": bool(settings.SWITCH_SYNC_ENABLED),
        "connection_sync": bool(settings.CONNECTION_SYNC_ENABLED),
        "tuya_events": bool(settings.TUYA_EVENTS_ENABLED),
        "motion": bool(settings.MOTION_DETECTION_ENABLED),
        "motion_cleanup": bool(settings.MOTION_CLEANUP_ENABLED),
        "ingest": bool(settings.INGEST_ENABLED),
        "hls": bool(settings.HLS_ENABLED),
        "webrtc": bool(settings.WEBRTC_ENABLED),
        "notify": True,
    }
