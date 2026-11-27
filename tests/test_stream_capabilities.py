import json
from types import SimpleNamespace

import pytest
from fastapi import Request

from app.core.config import settings
from app.core.errors import AppError
from app.routers.cams import hls, webrtc
from app.routers import config


def _request():
    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/v1/cams/1/hls/playlist.m3u8",
        "raw_path": b"/v1/cams/1/hls/playlist.m3u8", "query_string": b"",
        "headers": [], "client": ("203.0.113.4", 1234), "server": ("test", 80),
    })


def _cam():
    return SimpleNamespace(id=1, name="test", addr="camera", port=554, connection_resource_id=None)


@pytest.mark.asyncio
async def test_supported_streams_use_local_capability_in_standalone(monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", False)
    monkeypatch.setattr(settings, "HLS_ENABLED", True)
    monkeypatch.setattr(settings, "WEBRTC_ENABLED", True)
    monkeypatch.setattr(settings, "WEBRTC_STUN_SERVER", "")
    monkeypatch.setattr(settings, "_node_capabilities", {"hls": False, "webrtc": True})

    response = await config.get_supported_streams()
    formats = [row["format"] for row in json.loads(response.body)["data"]["streams"]]
    assert "hls" not in formats
    assert "webrtc" in formats


@pytest.mark.asyncio
async def test_supported_streams_use_local_capability_in_cluster(monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)
    monkeypatch.setattr(settings, "HLS_ENABLED", True)
    monkeypatch.setattr(settings, "WEBRTC_ENABLED", True)
    monkeypatch.setattr(settings, "WEBRTC_STUN_SERVER", "")
    monkeypatch.setattr(settings, "_node_capabilities", {"hls": False, "webrtc": False})

    response = await config.get_supported_streams()
    formats = [row["format"] for row in json.loads(response.body)["data"]["streams"]]
    assert {"hls", "webrtc"}.isdisjoint(formats)


@pytest.mark.asyncio
@pytest.mark.parametrize("cluster_enabled", [False, True])
async def test_hls_local_capability_blocks_playback(monkeypatch, cluster_enabled):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", cluster_enabled)
    monkeypatch.setattr(settings, "HLS_ENABLED", True)
    monkeypatch.setattr(hls, "_get_cam_for_access", lambda *_: _async_value(_cam()))
    monkeypatch.setattr(hls, "worker_enabled", lambda *_: False)
    monkeypatch.setattr(hls.HLS_BUDGET, "charge", _async_noop)

    with pytest.raises(AppError, match="HLS стриминг отключен"):
        await hls.get_hls_playlist(1, _request(), ctx=object(), db=object())


class _WebSocket:
    headers = {"user-agent": "pytest"}

    def __init__(self):
        self.closed = None

    async def close(self, code, reason):
        self.closed = (code, reason)


async def _async_value(value):
    return value


async def _async_noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("cluster_enabled", [False, True])
async def test_webrtc_local_capability_blocks_playback(monkeypatch, cluster_enabled):
    import app.deps.auth as auth

    monkeypatch.setattr(settings, "WEBRTC_ENABLED", True)
    monkeypatch.setattr(auth, "extract_token_flexible", lambda *_: "token")
    monkeypatch.setattr(auth, "authorize_device_token", lambda *_: _async_value(SimpleNamespace(uid=1)))
    monkeypatch.setattr(webrtc, "_get_cam_for_access", lambda *_: _async_value(_cam()))
    monkeypatch.setattr(webrtc.WEBRTC_BUDGET, "charge", _async_noop)
    monkeypatch.setattr(webrtc, "worker_enabled", lambda *_: False)

    monkeypatch.setattr(settings, "CLUSTER_ENABLED", cluster_enabled)
    ws = _WebSocket()
    await webrtc.webrtc_stream(ws, 1, "token", object())
    assert ws.closed and "отключён на этой ноде" in ws.closed[1]
