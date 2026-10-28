from __future__ import annotations

import asyncio
import contextlib
import hmac
import hashlib
import base64
import time
from dataclasses import dataclass
from urllib.parse import quote

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer, VideoStreamTrack
from aiortc.contrib.media import MediaBlackhole
from aiortc.mediastreams import MediaStreamError, VIDEO_TIME_BASE, VIDEO_CLOCK_RATE
from av import VideoFrame
import av

from app.core.config import settings
from app.models.cams import Cam


class WebRTCStreamError(Exception):
    pass


def generate_turn_credentials(secret: str, ttl: int | None = None) -> tuple[str, str]:
    """Generate temporary TURN credentials using coturn shared secret algorithm"""
    if ttl is None:
        ttl = settings.WEBRTC_TURN_CREDENTIAL_TTL_SECONDS
    timestamp = int(time.time()) + ttl
    username = str(timestamp)

    mac = hmac.new(
        secret.encode('utf-8'),
        username.encode('utf-8'),
        hashlib.sha1
    )
    password = base64.b64encode(mac.digest()).decode('utf-8')

    return username, password


def _with_transport(url: str, transport: str) -> str:
    """Add ?transport=... to a turn: URL that has none (browsers treat it as a hint)."""
    return url if "transport=" in url else f"{url}{'&' if '?' in url else '?'}transport={transport}"


def build_client_ice_servers() -> tuple[list[dict], int]:
    """ICE servers for the browser (GET /v1/webrtc/ice): STUN, TURN/udp with temporary
    credentials and optional turns/tcp. The shared secret never leaves the server. -> (servers, ttl)."""
    ttl = max(1, settings.WEBRTC_TURN_CREDENTIAL_TTL_SECONDS)
    servers: list[dict] = []

    if settings.WEBRTC_STUN_SERVER:
        servers.append({"urls": [settings.WEBRTC_STUN_SERVER]})

    if settings.WEBRTC_TURN_SECRET:
        urls = []
        if settings.WEBRTC_TURN_SERVER:
            urls.append(_with_transport(settings.WEBRTC_TURN_SERVER, "udp"))
        if settings.WEBRTC_TURNS_SERVER:
            urls.append(_with_transport(settings.WEBRTC_TURNS_SERVER, "tcp"))
        if urls:
            username, credential = generate_turn_credentials(settings.WEBRTC_TURN_SECRET, ttl)
            servers.append({"urls": urls, "username": username, "credential": credential})

    return servers, ttl


def get_ice_servers() -> list[RTCIceServer]:
    """Build ICE servers configuration from settings"""
    ice_servers = []

    # STUN server
    if settings.WEBRTC_STUN_SERVER:
        ice_servers.append(RTCIceServer(urls=[settings.WEBRTC_STUN_SERVER]))

    # TURN server with temporary credentials
    if settings.WEBRTC_TURN_SERVER and settings.WEBRTC_TURN_SECRET:
        username, credential = generate_turn_credentials(settings.WEBRTC_TURN_SECRET)
        ice_servers.append(
            RTCIceServer(
                urls=[settings.WEBRTC_TURN_SERVER],
                username=username,
                credential=credential,
            )
        )

    return ice_servers
