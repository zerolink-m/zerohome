"""RTSP URL helpers shared by every ffmpeg consumer (stream/HLS/WebRTC/ingest)."""
from __future__ import annotations

import re
from urllib.parse import quote

from app.models.cams import Cam

_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]*@")


def build_rtsp_url(cam: Cam) -> str:
    user = quote(cam.username, safe="")
    pwd = quote(cam.password, safe="")
    path = cam.path or ""
    if path and not path.startswith("/"):
        path = f"/{path}"
    host = f"[{cam.addr}]" if ":" in cam.addr and not cam.addr.startswith("[") else cam.addr
    return f"rtsp://{user}:{pwd}@{host}:{cam.port}{path}"


def mask_credentials(text: str) -> str:
    """Replaces `user:password@` in any URL inside `text` — safe for logs."""
    return _URL_CREDENTIALS.sub(r"\1***@", text)
