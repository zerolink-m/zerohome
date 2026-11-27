"""Cluster viewing stays on the node that receives the client request."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.models.node import Assignment, Node
from app.routers.cams import hls as hls_mod
from app.routers.cams import webrtc as webrtc_mod
from app.services import hls_manager, stream_manager
from tests.conftest import bearer, mint_test_token


PLAYLIST = "#EXTM3U\n#EXTINF:2.0,\nseg_1.ts\n"
SEGMENT = b"\x47" * (188 * 20)


@pytest.fixture
def cluster(monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)
    monkeypatch.setattr(settings, "HLS_ENABLED", True)
    monkeypatch.setattr(settings, "WEBRTC_ENABLED", True)
    monkeypatch.setattr(settings, "_node_capabilities", {})


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


async def test_hls_is_served_locally_on_a_non_owner(client, db, mk_user, mk_cam, cluster, hls_fake):
    owner_uid = await mk_user("o@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid)

    headers = await _token(owner_uid)
    playlist = await client.get(
        f"/v1/cams/{cid}/hls/playlist.m3u8", headers=headers, params={"token": "abc"},
    )
    assert playlist.status_code == 200, playlist.text
    assert "seg_1.ts?token=abc" in playlist.text
    assert "http://" not in playlist.text
    segment = await client.get(f"/v1/cams/{cid}/hls/seg_1.ts", headers=headers)
    assert segment.status_code == 200, segment.text
    assert segment.content == SEGMENT
    assert hls_fake == [cid, cid]


async def test_remote_lease_does_not_grant_access(client, db, mk_user, mk_cam, cluster, hls_fake):
    owner_uid = await mk_user("o@x.io")
    other_uid = await mk_user("other@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid)

    response = await client.get(f"/v1/cams/{cid}/hls/playlist.m3u8", headers=await _token(other_uid))
    assert response.status_code >= 400
    assert hls_fake == []


async def test_cluster_views_require_hub(cluster, monkeypatch):
    cam = SimpleNamespace(id=123)
    monkeypatch.setattr(hls_manager, "_hub_ready", AsyncMock(return_value=None))
    monkeypatch.setattr(stream_manager, "_hub_ready", AsyncMock(return_value=None))
    spawn = AsyncMock()
    monkeypatch.setattr(stream_manager, "_spawn", spawn)

    with pytest.raises(hls_manager.HLSStreamError, match="ingest"):
        await hls_manager.get_or_create_session(cam)
    with pytest.raises(stream_manager.StreamStartError, match="ingest"):
        await stream_manager.get_or_attach(cam)
    spawn.assert_not_awaited()


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


async def test_webrtc_signaling_and_ice_stay_on_non_owner(app, db, mk_user, mk_cam, cluster, monkeypatch):
    mgr = SimpleNamespace(
        create_session=AsyncMock(return_value=object()),
        create_offer=AsyncMock(return_value="v=0\r\nlocal-offer\r\n"),
        close_session=AsyncMock(),
        handle_answer=AsyncMock(),
        add_ice_candidate=AsyncMock(),
        WebRTCStreamError=Exception,
    )
    monkeypatch.setattr(webrtc_mod, "webrtc_manager", mgr)
    owner_uid = await mk_user("o@x.io")
    cid = await mk_cam(owner_uid)
    await _lease(db, cid)
    token = await mint_test_token(owner_uid)

    driver = _Driver(app, f"/v1/cams/{cid}/webrtc", f"token={token}")
    try:
        assert await driver.open()
        offer = json.loads(await driver.recv())
        assert offer["type"] == "offer" and "local-offer" in offer["sdp"]
        await driver.send_text(json.dumps({"type": "answer", "sdp": "client-answer"}))
        candidate = {"candidate": "candidate:1 1 UDP 1 192.0.2.1 4321 typ host", "sdpMLineIndex": 0}
        await driver.send_text(json.dumps({"type": "ice_candidate", "candidate": candidate}))
        for _ in range(50):
            if mgr.add_ice_candidate.await_count:
                break
            await asyncio.sleep(0.02)
        mgr.create_session.assert_awaited_once()
        session_id = mgr.create_session.await_args.args[1]
        mgr.handle_answer.assert_awaited_once_with(session_id, "client-answer")
        mgr.add_ice_candidate.assert_awaited_once_with(session_id, candidate)
    finally:
        await driver.close()
    mgr.close_session.assert_awaited_once()
