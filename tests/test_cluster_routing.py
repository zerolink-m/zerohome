"""HLS and WebRTC signaling follow the ingest lease. No sockets, no ffmpeg.

The non-owner app is the test client. The owner is a second in-process ASGI app,
or a fake httpx transport when the point is a refused connection or a slow body.
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlsplit

import httpx
import pytest
from starlette.requests import Request

from app.core.config import settings
from app.db.session import get_db
from app.main import create_app
from app.models.node import Assignment, Node
from app.routers.cams import hls as hls_mod
from app.routers.cams import webrtc as webrtc_mod
from app.services.cluster import view_proxy
from tests.conftest import bearer, mint_test_token

PLAYLIST = "#EXTM3U\n#EXTINF:2.0,\nseg_1.ts\n"
SEGMENT = b"\x47" * (188 * 20)


def _http(host, headers, path="/v1/cams/1/hls/playlist.m3u8", method="GET"):
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": raw, "client": (host, 9), "server": ("t", 80),
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _isolate_proxy(monkeypatch):
    monkeypatch.setattr(view_proxy, "http_transport_factory", None)
    monkeypatch.setattr(view_proxy, "ws_connector", None)


@pytest.fixture
def cluster(monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)
    monkeypatch.setattr(settings, "NODE_ID", "edge")
    monkeypatch.setattr(settings, "HLS_ENABLED", True)
    monkeypatch.setattr(settings, "CLUSTER_SECRET", "view-secret")
    monkeypatch.setattr(settings, "PORT", 8000)
    monkeypatch.setattr(settings, "CLUSTER_VIEW_HTTP_PORT", 0)


@pytest.fixture
def hls_fake(monkeypatch, tmp_path):
    (tmp_path / "seg_1.ts").write_bytes(SEGMENT)
    calls = []

    class _Sess:
        temp_dir = tmp_path

        def touch(self):
            pass

        def playlist_text(self):
            return PLAYLIST

    async def get_or_create(cam):
        calls.append(cam.id)
        return _Sess()

    monkeypatch.setattr(hls_mod.hls_manager, "get_or_create_session", get_or_create)
    monkeypatch.setattr(hls_mod.hls_manager, "schedule_idle_stop", lambda cam_id: None)
    return calls


def _owner_app(sessionmaker_):
    application = create_app()

    async def _get_db():
        async with sessionmaker_() as s:
            yield s

    application.dependency_overrides[get_db] = _get_db
    return application


def _record_transport(owner_app, seen, client_host="198.51.100.8"):
    """Upstream calls hit `owner_app`. The peer address is not loopback, so the
    hop is trusted only when the HMAC is valid."""

    def factory():
        class _T(httpx.ASGITransport):
            async def handle_async_request(self, request):
                seen.append(request)
                return await super().handle_async_request(request)

        return _T(app=owner_app, client=(client_host, 4040))

    return factory


async def _lease(db, cam_id, owner="owner-a", address="10.1.0.2:4700", ttl=60):
    now = int(time.time())
    async with db() as s:
        s.add(Node(
            node_id=owner, started_at=now, heartbeat_at=now,
            capabilities={"ingest": True}, address=address, version="t",
        ))
        s.add(Assignment(
            function="ingest", resource_id=cam_id, owner_node_id=owner,
            generation=1, expires_at=now + ttl, updated_at=now,
        ))
        await s.commit()


async def _token(uid):
    return bearer(await mint_test_token(uid))


def _as_starlette(req: httpx.Request, host: str) -> Request:
    query = req.url.query
    if isinstance(query, str):
        query = query.encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": req.method, "scheme": req.url.scheme, "path": req.url.path,
        "raw_path": req.url.path.encode(), "query_string": query,
        "headers": [(k.lower().encode(), v.encode()) for k, v in req.headers.items()],
        "client": (host, 1), "server": ("o", 80),
    }
    return Request(scope)


def test_proxy_header_trust_rules(monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)
    monkeypatch.setattr(settings, "CLUSTER_SECRET", "s3cret")
    path = "/v1/cams/7/hls/playlist.m3u8"
    ts = int(time.time())
    sig = view_proxy.sign_proxy("s3cret", "GET", path, ts)
    good = {
        "x-zerocams-proxied": "1",
        "x-zerocams-proxy-ts": str(ts),
        "x-zerocams-proxy-sig": sig,
    }
    assert view_proxy.trusted_proxied(_http("203.0.113.9", good, path=path))
    assert view_proxy.trusted_proxied(_http("::ffff:127.0.0.1", {"x-zerocams-proxied": "1"}, path=path))
    assert not view_proxy.trusted_proxied(_http("203.0.113.9", {"x-zerocams-proxied": "1"}, path=path))
    bad = dict(good, **{"x-zerocams-proxy-sig": "0" * 64})
    assert not view_proxy.trusted_proxied(_http("203.0.113.9", bad, path=path))
    stale = dict(good, **{"x-zerocams-proxy-ts": str(ts - 31)})
    stale["x-zerocams-proxy-sig"] = view_proxy.sign_proxy("s3cret", "GET", path, ts - 31)
    assert not view_proxy.trusted_proxied(_http("203.0.113.9", stale, path=path))
    other = dict(good)
    other["x-zerocams-proxy-sig"] = view_proxy.sign_proxy("s3cret", "GET", "/v1/cams/8/hls/playlist.m3u8", ts)
    assert not view_proxy.trusted_proxied(_http("203.0.113.9", other, path=path))
    monkeypatch.setattr(settings, "CLUSTER_SECRET", "")
    assert view_proxy.trusted_proxied(_http("127.0.0.1", {"x-zerocams-proxied": "1"}))
    assert not view_proxy.trusted_proxied(_http("203.0.113.9", good))
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", False)
    assert not view_proxy.trusted_proxied(_http("127.0.0.1", {"x-zerocams-proxied": "1"}))


async def test_hls_on_non_owner_is_proxied_and_playlist_stays_relative(
    app, client, db, mk_user, mk_cam, sessionmaker_, cluster, hls_fake, monkeypatch,
):
    owner_uid = await mk_user("o@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid, address="10.1.0.2:4700")
    seen = []
    monkeypatch.setattr(view_proxy, "http_transport_factory", _record_transport(_owner_app(sessionmaker_), seen))
    headers = await _token(owner_uid)
    r = await client.get(f"/v1/cams/{cid}/hls/playlist.m3u8", headers=headers, params={"token": "abc"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert "no-store" in r.headers["cache-control"]
    body = r.text
    assert "seg_1.ts?token=abc" in body
    assert "10.1.0.2" not in body and "http://" not in body
    assert len(seen) == 1
    assert seen[0].url.host == "10.1.0.2"
    assert seen[0].url.port == 8000  # HTTP port, not the fMP4 port written in the address
    assert hls_fake == [cid]  # only the owner opened a session
    hop = _as_starlette(seen[0], "198.51.100.8")
    assert view_proxy.trusted_proxied(hop)


async def test_hls_segment_bytes_come_from_owner(
    app, client, db, mk_user, mk_cam, sessionmaker_, cluster, hls_fake, monkeypatch,
):
    owner_uid = await mk_user("o@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid)
    seen = []
    monkeypatch.setattr(view_proxy, "http_transport_factory", _record_transport(_owner_app(sessionmaker_), seen))
    r = await client.get(f"/v1/cams/{cid}/hls/seg_1.ts", headers=await _token(owner_uid))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("video/mp2t")
    assert r.content == SEGMENT
    assert "max-age" in r.headers["cache-control"]
    assert len(seen) == 1 and hls_fake == [cid]


async def test_segment_is_streamed_not_buffered(app, db, mk_user, mk_cam, cluster, monkeypatch):
    owner_uid = await mk_user("o@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid)
    gate = asyncio.Event()

    class _Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"A" * 100
            await gate.wait()
            yield b"B" * 100

        async def aclose(self):
            return None

    class _Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, headers={"content-type": "video/mp2t"}, stream=_Slow())

    monkeypatch.setattr(view_proxy, "http_transport_factory", lambda: _Transport())
    token = await mint_test_token(owner_uid)
    path = f"/v1/cams/{cid}/hls/seg_1.ts"
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"user-agent", b"pytest-UA/1.0"),
            (b"authorization", f"Bearer {token}".encode()),
        ],
        "client": ("203.0.113.9", 9), "server": ("test", 80),
    }
    chunks = []
    first = asyncio.Event()
    done = asyncio.Event()

    async def receive():
        if not receive.sent:
            receive.sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    receive.sent = False

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            chunks.append(message["body"])
            first.set()
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            done.set()

    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(first.wait(), 2)
    assert chunks == [b"A" * 100]
    assert not gate.is_set()
    gate.set()
    await asyncio.wait_for(task, 2)
    assert b"".join(chunks) == b"A" * 100 + b"B" * 100


async def test_owner_node_serves_locally_without_proxy(
    app, client, db, mk_user, mk_cam, cluster, hls_fake, monkeypatch,
):
    monkeypatch.setattr(settings, "NODE_ID", "owner-a")
    owner_uid = await mk_user("o@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid)

    def boom():
        raise AssertionError("proxied a request this node owns")

    monkeypatch.setattr(view_proxy, "http_transport_factory", boom)
    r = await client.get(f"/v1/cams/{cid}/hls/playlist.m3u8", headers=await _token(owner_uid))
    assert r.status_code == 200, r.text
    assert "seg_1.ts" in r.text
    assert hls_fake == [cid]


async def test_missing_or_blank_owner_stays_local(
    app, client, db, mk_user, mk_cam, cluster, hls_fake, monkeypatch,
):
    owner_uid = await mk_user("o@x.io")
    cid = await mk_cam(owner_uid)
    def boom():
        raise AssertionError("proxied without an owner")

    monkeypatch.setattr(view_proxy, "http_transport_factory", boom)
    r = await client.get(f"/v1/cams/{cid}/hls/playlist.m3u8", headers=await _token(owner_uid))
    assert r.status_code == 200, r.text
    assert hls_fake == [cid]

    hls_fake.clear()
    await _lease(db, cid, address="")
    r = await client.get(f"/v1/cams/{cid}/hls/playlist.m3u8", headers=await _token(owner_uid))
    assert r.status_code == 200, r.text
    assert hls_fake == [cid]

    hls_fake.clear()
    now = int(time.time())
    async with db() as s:
        await s.execute(Node.__table__.update().values(address="10.9.9.9"))
        await s.execute(Assignment.__table__.update().values(expires_at=now - 5))
        await s.commit()
    r = await client.get(f"/v1/cams/{cid}/hls/playlist.m3u8", headers=await _token(owner_uid))
    assert r.status_code == 200, r.text
    assert hls_fake == [cid]


async def test_untrusted_proxy_header_is_ignored_loopback_is_local(
    app, client, db, mk_user, mk_cam, sessionmaker_, cluster, hls_fake, monkeypatch,
):
    owner_uid = await mk_user("o@x.io")
    stranger = await mk_user("s@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid)
    seen = []
    monkeypatch.setattr(view_proxy, "http_transport_factory", _record_transport(_owner_app(sessionmaker_), seen))
    forged = {"x-zerocams-proxied": "1"}

    denied = await client.get(
        f"/v1/cams/{cid}/hls/playlist.m3u8", headers={**await _token(stranger), **forged},
    )
    assert denied.status_code != 200
    assert denied.json()["code"] == "082"
    assert seen == [] and hls_fake == []

    proxied = await client.get(
        f"/v1/cams/{cid}/hls/playlist.m3u8", headers={**await _token(owner_uid), **forged},
    )
    assert proxied.status_code == 200, proxied.text
    assert len(seen) == 1 and hls_fake == [cid]

    hls_fake.clear()
    seen.clear()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 7))
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers={"user-agent": "pytest-UA/1.0"}) as local:
        # No token: loopback + the hop header is already authorized, and it is not proxied.
        direct = await local.get(f"/v1/cams/{cid}/hls/playlist.m3u8", headers=forged)
    assert direct.status_code == 200, direct.text
    assert "seg_1.ts" in direct.text
    assert seen == [] and hls_fake == [cid]


async def test_unreachable_owner_is_503_not_a_hang(
    app, client, db, mk_user, mk_cam, cluster, monkeypatch,
):
    owner_uid = await mk_user("o@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid)
    headers = await _token(owner_uid)

    class _Refuse(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(view_proxy, "http_transport_factory", lambda: _Refuse())
    refused = await client.get(f"/v1/cams/{cid}/hls/playlist.m3u8", headers=headers)
    assert refused.status_code == 503
    body = refused.json()
    assert body["code"] == "225"
    assert body["error"]["hint"] == "Повторите запрос"

    class _Hang(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(30)
            return httpx.Response(200)

    monkeypatch.setattr(settings, "CLUSTER_VIEW_CONNECT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(settings, "CLUSTER_VIEW_READ_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(view_proxy, "http_transport_factory", lambda: _Hang())
    started = time.monotonic()
    hung = await client.get(f"/v1/cams/{cid}/hls/playlist.m3u8", headers=headers)
    assert time.monotonic() - started < 2
    assert hung.status_code == 503 and hung.json()["code"] == "225"


async def test_unsigned_non_loopback_owner_is_not_dialed(
    app, client, db, mk_user, mk_cam, cluster, monkeypatch,
):
    monkeypatch.setattr(settings, "CLUSTER_SECRET", "")
    owner_uid = await mk_user("o@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid, address="10.1.0.2")

    def boom():
        raise AssertionError("dialed a non-loopback owner without CLUSTER_SECRET")

    monkeypatch.setattr(view_proxy, "http_transport_factory", boom)
    r = await client.get(f"/v1/cams/{cid}/hls/playlist.m3u8", headers=await _token(owner_uid))
    assert r.status_code == 503 and r.json()["code"] == "225"


async def test_cluster_disabled_does_not_proxy(
    app, client, db, mk_user, mk_cam, hls_fake, monkeypatch,
):
    monkeypatch.setattr(settings, "HLS_ENABLED", True)
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", False)
    monkeypatch.setattr(settings, "NODE_ID", "edge")
    owner_uid = await mk_user("o@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid)

    def boom():
        raise AssertionError("proxy ran with CLUSTER_ENABLED=false")

    monkeypatch.setattr(view_proxy, "http_transport_factory", boom)
    r = await client.get(f"/v1/cams/{cid}/hls/playlist.m3u8", headers=await _token(owner_uid))
    assert r.status_code == 200, r.text
    assert r.text.startswith("#EXTM3U")
    assert hls_fake == [cid]

    naked = await client.get(
        f"/v1/cams/{cid}/hls/playlist.m3u8", headers={"x-zerocams-proxied": "1"},
    )
    assert naked.status_code != 200
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 7))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as local:
        loop = await local.get(
            f"/v1/cams/{cid}/hls/playlist.m3u8", headers={"x-zerocams-proxied": "1"},
        )
    assert loop.status_code != 200


class _Upstream:
    """In-process client of the owner ASGI app."""

    def __init__(self, application, url, headers):
        parts = urlsplit(url)
        self._in = asyncio.Queue()
        self._out = asyncio.Queue()
        self._closed = False
        scope = {
            "type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "scheme": "ws", "path": parts.path, "raw_path": parts.path.encode(),
            "query_string": parts.query.encode(), "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()] + [(b"user-agent", b"pytest-UA/1.0")],
            "client": ("198.51.100.8", 9), "server": ("owner", 80), "subprotocols": [],
        }
        self._task = asyncio.create_task(application(scope, self._receive, self._send))

    async def _receive(self):
        return await self._in.get()

    async def _send(self, message):
        await self._out.put(message)

    async def start(self):
        await self._in.put({"type": "websocket.connect"})
        msg = await asyncio.wait_for(self._out.get(), 5)
        if msg["type"] == "websocket.close":
            raise view_proxy.OwnerUnreachable(str(msg.get("code")))
        if msg["type"] != "websocket.accept":
            raise view_proxy.OwnerUnreachable(msg["type"])

    async def send(self, data):
        if isinstance(data, str):
            await self._in.put({"type": "websocket.receive", "text": data})
        else:
            await self._in.put({"type": "websocket.receive", "bytes": data})

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self._out.get()
        if msg["type"] == "websocket.send":
            if msg.get("text") is not None:
                return msg["text"]
            return msg.get("bytes") or b""
        if msg["type"] == "websocket.close":
            raise StopAsyncIteration
        return await self.__anext__()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await self._in.put({"type": "websocket.disconnect", "code": 1000})
        if not self._task.done():
            try:
                await asyncio.wait_for(self._task, 2)
            except (asyncio.TimeoutError, TimeoutError):
                self._task.cancel()


class _Driver:
    def __init__(self, application, path, query, host="203.0.113.9"):
        self._in = asyncio.Queue()
        self._out = asyncio.Queue()
        self.close_code = None
        scope = {
            "type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "scheme": "ws", "path": path, "raw_path": path.encode(),
            "query_string": query.encode(), "root_path": "",
            "headers": [(b"host", b"test"), (b"user-agent", b"pytest-UA/1.0")],
            "client": (host, 1), "server": ("test", 80), "subprotocols": [],
        }
        self._task = asyncio.create_task(application(scope, self._receive, self._send))

    async def _receive(self):
        return await self._in.get()

    async def _send(self, message):
        await self._out.put(message)

    async def open(self):
        await self._in.put({"type": "websocket.connect"})
        msg = await asyncio.wait_for(self._out.get(), 5)
        if msg["type"] == "websocket.close":
            self.close_code = msg.get("code")
            return False
        return msg["type"] == "websocket.accept"

    async def recv(self):
        msg = await asyncio.wait_for(self._out.get(), 5)
        assert msg["type"] == "websocket.send", msg
        return msg.get("text")

    async def send_text(self, text):
        await self._in.put({"type": "websocket.receive", "text": text})

    async def close(self):
        await self._in.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self._task, 5)
        except (asyncio.TimeoutError, TimeoutError):
            self._task.cancel()


async def test_webrtc_signaling_is_proxied_both_ways(
    app, db, mk_user, mk_cam, sessionmaker_, cluster, monkeypatch,
):
    monkeypatch.setattr(settings, "WEBRTC_ENABLED", True)
    owner_app = _owner_app(sessionmaker_)
    answered = []

    async def handle_answer(session_id, sdp):
        answered.append(sdp)

    from unittest.mock import AsyncMock

    mgr = type("M", (), {})()
    mgr.create_session = AsyncMock(return_value=object())
    mgr.create_offer = AsyncMock(return_value="v=0\r\nowner-offer\r\n")
    mgr.close_session = AsyncMock()
    mgr.handle_answer = handle_answer
    mgr.add_ice_candidate = AsyncMock()
    mgr.WebRTCStreamError = Exception
    monkeypatch.setattr(webrtc_mod, "webrtc_manager", mgr)

    owner_uid = await mk_user("o@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid)
    token = await mint_test_token(owner_uid)

    async def connect(url, headers):
        upstream = _Upstream(owner_app, url, headers)
        await upstream.start()
        assert url.startswith(f"ws://10.1.0.2:8000/v1/cams/{cid}/webrtc")
        assert headers.get("x-zerocams-proxied") == "1"
        assert "x-zerocams-proxy-sig" in headers
        return upstream

    monkeypatch.setattr(view_proxy, "ws_connector", connect)
    driver = _Driver(app, f"/v1/cams/{cid}/webrtc", f"token={token}")
    assert await driver.open()
    offer = json.loads(await driver.recv())
    assert offer["type"] == "offer" and "owner-offer" in offer["sdp"]
    await driver.send_text(json.dumps({"type": "answer", "sdp": "client-answer"}))
    for _ in range(50):
        if answered:
            break
        await asyncio.sleep(0.02)
    assert answered == ["client-answer"]
    assert mgr.create_session.await_count == 1  # owner only; the edge did not start a peer
    await driver.close()
    mgr.close_session.assert_awaited()


async def test_webrtc_unreachable_owner_closes_instead_of_hanging(
    app, db, mk_user, mk_cam, cluster, monkeypatch,
):
    monkeypatch.setattr(settings, "WEBRTC_ENABLED", True)
    owner_uid = await mk_user("o@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid)
    token = await mint_test_token(owner_uid)

    async def connect(url, headers):
        raise view_proxy.OwnerUnreachable("down")

    monkeypatch.setattr(view_proxy, "ws_connector", connect)
    started = time.monotonic()
    driver = _Driver(app, f"/v1/cams/{cid}/webrtc", f"token={token}")
    opened = await driver.open()
    assert not opened and driver.close_code == 1013
    assert time.monotonic() - started < 2
    await driver.close()
