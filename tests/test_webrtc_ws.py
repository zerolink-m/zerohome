"""WebRTC websocket: camera access must follow the same rules as stream/ptz."""
from tests.conftest import mint_test_token
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.core.security import create_share_token
from app.models.invitation import Invitation
from app.models.sharing import Sharing
from app.routers.cams import webrtc as webrtc_mod


async def ws_connect(app, cam_id, token):
    """Minimal ASGI websocket driver. Returns (accepted, close_code, sent_messages)."""
    qs = f"token={token}".encode()
    scope = {
        "type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "scheme": "ws", "path": f"/v1/cams/{cam_id}/webrtc", "raw_path": b"",
        "query_string": qs, "root_path": "", "headers": [(b"host", b"test"), (b"user-agent", b"pytest-UA/1.0")],
        "client": ("203.0.113.9", 1), "server": ("test", 80), "subprotocols": [],
    }
    inbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "websocket.connect"})
    out = {"accepted": False, "close": None, "sent": []}
    done = asyncio.Event()

    async def receive():
        if inbox.empty() and out["accepted"]:
            await done.wait()
            return {"type": "websocket.disconnect", "code": 1000}
        return await inbox.get()

    async def send(msg):
        if msg["type"] == "websocket.accept":
            out["accepted"] = True
        elif msg["type"] == "websocket.close":
            out["close"] = msg.get("code")
        elif msg["type"] == "websocket.send":
            out["sent"].append(msg.get("text"))
            done.set()  # got the offer -> client hangs up

    await asyncio.wait_for(app(scope, receive, send), 5)
    return out["accepted"], out["close"], out["sent"]


@pytest.fixture
def fake_rtc(monkeypatch):
    monkeypatch.setattr(settings, "WEBRTC_ENABLED", True)
    mgr = type("M", (), {})()
    mgr.create_session = AsyncMock(return_value=object())
    mgr.create_offer = AsyncMock(return_value="v=0 fake")
    mgr.close_session = AsyncMock()
    mgr.handle_answer = AsyncMock()
    mgr.add_ice_candidate = AsyncMock()
    mgr.WebRTCStreamError = Exception
    monkeypatch.setattr(webrtc_mod, "webrtc_manager", mgr)
    return mgr


async def tok(uid, role="user"):
    return await mint_test_token(uid, role)


async def test_webrtc_stranger_rejected(app, mk_user, mk_cam, fake_rtc):
    owner, other = await mk_user("o@x.io"), await mk_user("s@x.io")
    cid = await mk_cam(owner)
    ok, code, _ = await ws_connect(app, cid, await tok(other))
    assert not ok and code == 1008
    fake_rtc.create_session.assert_not_called()


async def test_webrtc_junk_token_and_missing_cam(app, mk_user, mk_cam, fake_rtc):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    assert (await ws_connect(app, cid, "junk"))[:2] == (False, 1008)
    assert (await ws_connect(app, 9999, await tok(owner)))[:2] == (False, 1008)


@pytest.mark.parametrize("who", ["owner", "admin"])
async def test_webrtc_owner_and_admin_allowed(app, mk_user, mk_cam, fake_rtc, who):
    owner = await mk_user("o@x.io")
    admin = await mk_user("a@x.io", role="admin")
    cid = await mk_cam(owner)
    t = await tok(owner) if who == "owner" else await tok(admin, "admin")
    ok, code, sent = await ws_connect(app, cid, t)
    assert ok and sent and "offer" in sent[0]
    fake_rtc.create_session.assert_awaited_once()


async def test_webrtc_invited_allowed_only_when_accepted_and_enabled(app, db, mk_user, mk_cam, fake_rtc):
    owner, guest = await mk_user("o@x.io"), await mk_user("g@x.io")
    cid = await mk_cam(owner)
    async with db() as s:
        s.add(Invitation(device_type="cams", device_id=cid, email="g@x.io", invited_by=owner,
                         status="pending", added_at=int(time.time())))
        await s.commit()
    assert (await ws_connect(app, cid, await tok(guest)))[:2] == (False, 1008)
    async with db() as s:
        await s.execute(Invitation.__table__.update().values(status="accepted"))
        await s.commit()
    ok, _, _ = await ws_connect(app, cid, await tok(guest))
    assert ok


async def test_webrtc_share_token(app, db, mk_user, mk_cam, fake_rtc):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    cid2 = await mk_cam(owner)
    async with db() as s:
        row = Sharing(uid=owner, device_type="cams", device_id=cid, type="stream", added_at=int(time.time()), updated_at=int(time.time()))
        s.add(row)
        await s.commit()
        sha = row.id
    t = create_share_token(sha, owner, "stream", "cams", cid, None, 600)
    ok, _, _ = await ws_connect(app, cid, t)
    assert ok
    assert (await ws_connect(app, cid2, t))[:2] == (False, 1008)  # other cam
    async with db() as s:
        await s.delete(await s.get(Sharing, sha))
        await s.commit()
    assert (await ws_connect(app, cid, t))[:2] == (False, 1008)  # revoked


async def test_webrtc_connection_rate_limited(app, mk_user, mk_cam, fake_rtc):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    res = [await ws_connect(app, cid, await tok(owner)) for _ in range(webrtc_mod.WEBRTC_BUDGET.capacity + 1)]
    assert all(r[0] for r in res[:-1])
    assert res[-1][:2] == (False, 1008)
