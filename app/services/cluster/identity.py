"""Node identity and local capabilities."""
from __future__ import annotations

import re
import secrets
import socket
import os
import json
from pathlib import Path
from typing import Any

_generated_id: str | None = None
_identity_pending_registration = False
IDENTITY_FILE = Path("/data/zerocams/node-id")


def _persistent_id() -> str:
    """Read or atomically create this installation's stable local identity."""
    global _identity_pending_registration
    IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = IDENTITY_FILE.read_text(encoding="utf-8").strip()
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            record = None
        if isinstance(record, dict):
            value = str(record.get("node_id") or "").strip()
            _identity_pending_registration = record.get("state") == "pending"
        else:
            value = raw
            _identity_pending_registration = False
        if value:
            return value[:64]
        raise RuntimeError(f"Local node identity file is empty: {IDENTITY_FILE}")
    except FileNotFoundError:
        pass
    requested = os.environ.get("BOOTSTRAP_NODE_ID", "").strip()
    if requested and len(requested) > 64:
        raise RuntimeError("BOOTSTRAP_NODE_ID must be at most 64 characters")
    value = requested or f"{(socket.gethostname() or 'node')[:52]}-{secrets.token_hex(5)}"
    temp = IDENTITY_FILE.with_name(f".node-id-{secrets.token_hex(6)}")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as identity:
            identity.write(json.dumps({"node_id": value, "state": "pending"}) + "\n")
            identity.flush()
            os.fsync(identity.fileno())
        os.link(temp, IDENTITY_FILE)
    except FileExistsError:
        return _persistent_id()
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    _identity_pending_registration = True
    return value


def identity_registration_pending() -> bool:
    """Whether identity creation was durable before its first DB registration."""
    if _generated_id is None:
        _persistent_id()
    return _identity_pending_registration


def mark_identity_registered() -> None:
    """Persist the registration marker only after the DB transaction commits."""
    global _identity_pending_registration
    if not _identity_pending_registration:
        return
    record = json.dumps({"node_id": get_node_id(), "state": "registered"}) + "\n"
    temp = IDENTITY_FILE.with_name(f".node-id-{secrets.token_hex(6)}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as identity:
            identity.write(record)
            identity.flush()
            os.fsync(identity.fileno())
        os.replace(temp, IDENTITY_FILE)
        _identity_pending_registration = False
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def get_node_id() -> str:
    """Return the stable identity read from or created in the local identity file."""
    global _generated_id
    if _generated_id is None:
        _generated_id = _persistent_id()
    return _generated_id


def safe_node_dir_name(node_id: str) -> str:
    """Filesystem-safe form of a node id: only [A-Za-z0-9._-], never empty, never '.'/'..'."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", node_id or "")
    name = name.replace("..", "__").strip(".") or "node"
    return name[:64]


def local_capabilities(settings: Any) -> dict[str, bool]:
    """Effective worker permissions: global switch AND this node's DB capability."""
    global_switches = {
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
        "notify": bool(settings.NOTIFY_ENABLED),
    }
    enabled = getattr(settings, "_node_capabilities", {}) or {}
    return {name: bool(value and enabled.get(name, True)) for name, value in global_switches.items()}


def worker_enabled(settings: Any, name: str) -> bool:
    """Whether a local worker may run under both global and node-local policy."""
    return local_capabilities(settings).get(name, False)


def stream_advertised(settings: Any, name: str) -> bool:
    """Standalone advertises only locally enabled streams; cluster ingress may proxy.

    The actual owner still applies its own local capability before serving a stream.
    """
    if settings.CLUSTER_ENABLED:
        return bool(getattr(settings, f"{name.upper()}_ENABLED", False))
    return worker_enabled(settings, name)


def supported_capabilities() -> dict[str, bool]:
    """Capabilities implemented by this application build, enabled on first registration."""
    return {
        "session_cleanup": True,
        "healthcheck": True,
        "switch_sync": True,
        "connection_sync": True,
        "tuya_events": True,
        "motion": True,
        "motion_cleanup": True,
        "ingest": True,
        "hls": True,
        "webrtc": True,
        "notify": True,
    }
