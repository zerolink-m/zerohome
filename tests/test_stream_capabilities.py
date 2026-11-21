import json
from types import SimpleNamespace

import pytest
from fastapi import Request

from app.core.config import settings
from app.core.errors import AppError
from app.routers.cams import hls, webrtc
from app.routers import config
from app.services.cluster import identity, view_proxy


def _request():
    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/v1/cams/1/hls/playlist.m3u8",
        "raw_path": b"/v1/cams/1/hls/playlist.m3u8", "query_string": b"",
        "headers": [], "client": ("203.0.113.4", 1234), "server": ("test", 80),
    })


def _cam():
    return SimpleNamespace(id=1, name="test", addr="camera", port=554)


@pytest.mark.asyncio
async def test_supported_streams_use_local_capability_in_standalone(monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", False)
    monkeypatch.setattr(settings, "HLS_ENABLED", True)
    monkeypatch.setattr(settings, "WEBRTC_ENABLED", True)
    monkeypatch.setattr(settings, "WEBRTC_STUN_SERVER", "stun:example.test")
    monkeypatch.setattr(settings, "_node_capabilities", {"hls": False, "webrtc": True})

    response = await config.get_supported_streams()
    formats = [row["format"] for row in json.loads(response.body)["data"]["streams"]]
    assert "hls" not in formats
    assert "webrtc" in formats


@pytest.mark.asyncio
async def test_supported_streams_keep_cluster_ingress_proxy_options(monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)
    monkeypatch.setattr(settings, "HLS_ENABLED", True)
    monkeypatch.setattr(settings, "WEBRTC_ENABLED", True)
    monkeypatch.setattr(settings, "WEBRTC_STUN_SERVER", "stun:example.test")
    monkeypatch.setattr(settings, "_node_capabilities", {"hls": False, "webrtc": False})

    response = await config.get_supported_streams()
    formats = [row["format"] for row in json.loads(response.body)["data"]["streams"]]
    assert {"hls", "webrtc"} <= set(formats)


@pytest.mark.asyncio
async def test_hls_local_capability_blocks_standalone_but_not_proxy(monkeypatch):
    monkeypatch.setattr(settings, "HLS_ENABLED", True)
    monkeypatch.setattr(hls, "_cam_for_hls", lambda *_: _async_value(_cam()))
    monkeypatch.setattr(hls, "worker_enabled", lambda *_: False)

    with pytest.raises(AppError, match="HLS стриминг отключен"):
        await hls.get_hls_playlist(1, _request(), ctx=None, db=object())

    monkeypatch.setattr(hls.HLS_BUDGET, "charge", _async_noop)
    monkeypatch.setattr(view_proxy, "view_target", lambda *_: _async_value(SimpleNamespace(host="owner")))
    proxied = object()
    monkeypatch.setattr(view_proxy, "proxy_http", lambda *_: _async_value(proxied))
    result = await hls.get_hls_playlist(1, _request(), ctx=object(), db=object())
    assert result is proxied


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
async def test_webrtc_local_capability_blocks_owner_but_not_cluster_proxy(monkeypatch):
    import app.deps.auth as auth

    monkeypatch.setattr(settings, "WEBRTC_ENABLED", True)
    monkeypatch.setattr(view_proxy, "trusted_proxied", lambda _conn: False)
    monkeypatch.setattr(auth, "extract_token_flexible", lambda *_: "token")
    monkeypatch.setattr(auth, "authorize_device_token", lambda *_: _async_value(SimpleNamespace(uid=1)))
    monkeypatch.setattr(webrtc, "_get_cam_for_access", lambda *_: _async_value(_cam()))
    monkeypatch.setattr(webrtc.WEBRTC_BUDGET, "charge", _async_noop)
    monkeypatch.setattr(webrtc, "worker_enabled", lambda *_: False)

    monkeypatch.setattr(settings, "CLUSTER_ENABLED", False)
    ws = _WebSocket()
    await webrtc.webrtc_stream(ws, 1, "token", object())
    assert ws.closed and "отключён на этой ноде" in ws.closed[1]

    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)
    monkeypatch.setattr(view_proxy, "view_target", lambda *_: _async_value(SimpleNamespace(host="owner")))
    proxied = []
    async def _proxy(*_args):
        proxied.append(True)
    monkeypatch.setattr(view_proxy, "proxy_websocket", _proxy)
    ws = _WebSocket()
    await webrtc.webrtc_stream(ws, 1, "token", object())
    assert proxied == [True]
    assert ws.closed is None
