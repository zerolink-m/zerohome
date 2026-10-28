"""GET /v1/webrtc/ice, private-address hiding in SDP, public candidate, UDP port range."""
import base64
import hashlib
import hmac
import ipaddress
import re
import time

import aioice.ice
import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, VideoStreamTrack

from app.core.config import settings
from app.core.security import create_access_token, create_session_token, create_share_token
from app.models.sharing import Sharing
from app.services.webrtc_manager import portrange, sdp as sdp_mod
from app.services.webrtc_manager.sdp import is_private_address, sanitize_sdp, sanitize_local_sdp

SECRET = "unit-test-turn-secret"


def hdr(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def turn_cfg(monkeypatch):
    monkeypatch.setattr(settings, "WEBRTC_STUN_SERVER", "stun:turn.example.org:3478")
    monkeypatch.setattr(settings, "WEBRTC_TURN_SERVER", "turn:turn.example.org:3478")
    monkeypatch.setattr(settings, "WEBRTC_TURN_SECRET", SECRET)
    monkeypatch.setattr(settings, "WEBRTC_TURNS_SERVER", "")
    monkeypatch.setattr(settings, "WEBRTC_TURN_CREDENTIAL_TTL_SECONDS", 3600)


# --- endpoint ----------------------------------------------------------------------

async def test_ice_requires_auth(client, turn_cfg):
    r = await client.get("/v1/webrtc/ice")
    assert r.json()["code"] == "046" and r.status_code >= 400
    r = await client.get("/v1/webrtc/ice", headers=hdr("junk"))
    assert r.status_code >= 400 and r.json()["code"] != "000"


async def test_ice_rejects_session_token(client, turn_cfg):
    r = await client.get("/v1/webrtc/ice", headers=hdr(create_session_token(1, 1)))
    assert r.status_code == 401


async def test_ice_with_turn(client, mk_user, turn_cfg):
    uid = await mk_user("a@x.io")
    before = int(time.time())
    r = await client.get("/v1/webrtc/ice", headers=hdr(create_access_token(uid, 1, "user")))
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == "000"
    ice = body["data"]["ice"]
    assert ice["ttl"] == 3600
    stun, turn = ice["ice_servers"]
    assert stun == {"urls": ["stun:turn.example.org:3478"]}
    assert turn["urls"] == ["turn:turn.example.org:3478?transport=udp"]
    # coturn use-auth-secret: username = expiry timestamp, credential = base64(hmac-sha1)
    assert before + 3600 <= int(turn["username"]) <= int(time.time()) + 3600
    expected = base64.b64encode(hmac.new(SECRET.encode(), turn["username"].encode(), hashlib.sha1).digest()).decode()
    assert turn["credential"] == expected
    assert SECRET not in r.text


async def test_ice_admin_ttl_and_turns(client, mk_user, turn_cfg, monkeypatch):
    monkeypatch.setattr(settings, "WEBRTC_TURN_CREDENTIAL_TTL_SECONDS", 120)
    monkeypatch.setattr(settings, "WEBRTC_TURNS_SERVER", "turns:turn.example.org:5349")
    uid = await mk_user("adm@x.io", role="admin")
    r = await client.get("/v1/webrtc/ice", headers=hdr(create_access_token(uid, 1, "admin")))
    ice = r.json()["data"]["ice"]
    turn = ice["ice_servers"][1]
    assert ice["ttl"] == 120
    assert turn["urls"] == ["turn:turn.example.org:3478?transport=udp", "turns:turn.example.org:5349?transport=tcp"]
    assert int(turn["username"]) <= int(time.time()) + 120
    assert SECRET not in r.text


async def test_ice_stun_only_and_empty(client, mk_user, turn_cfg, monkeypatch):
    uid = await mk_user("a@x.io")
    h = hdr(create_access_token(uid, 1, "user"))
    monkeypatch.setattr(settings, "WEBRTC_TURN_SECRET", "")
    ice = (await client.get("/v1/webrtc/ice", headers=h)).json()["data"]["ice"]
    assert ice["ice_servers"] == [{"urls": ["stun:turn.example.org:3478"]}]
    monkeypatch.setattr(settings, "WEBRTC_STUN_SERVER", "")
    ice = (await client.get("/v1/webrtc/ice", headers=h)).json()["data"]["ice"]
    assert ice["ice_servers"] == []


async def test_ice_share_token(client, db, mk_user, mk_cam, turn_cfg):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    now = int(time.time())
    async with db() as s:
        row = Sharing(uid=owner, device_type="cams", device_id=cid, type="stream", added_at=now, updated_at=now)
        rec = Sharing(uid=owner, device_type="cams", device_id=cid, type="record", added_at=now, updated_at=now)
        s.add_all([row, rec])
        await s.commit()
        sha, rec_id = row.id, rec.id
    ok = create_share_token(sha, owner, "stream", "cams", cid, None, 600)
    assert (await client.get("/v1/webrtc/ice", headers=hdr(ok))).status_code == 200
    wrong = create_share_token(rec_id, owner, "record", "cams", cid, "f.mp4", 600)
    assert (await client.get("/v1/webrtc/ice", headers=hdr(wrong))).status_code == 401
    async with db() as s:
        await s.delete(await s.get(Sharing, sha))
        await s.commit()
    assert (await client.get("/v1/webrtc/ice", headers=hdr(ok))).status_code == 401  # revoked


async def test_ice_rate_limited(client, mk_user, turn_cfg):
    uid = await mk_user("a@x.io")
    h = hdr(create_access_token(uid, 1, "user"))
    codes = [(await client.get("/v1/webrtc/ice", headers=h)).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


def test_client_servers_dont_touch_server_side_config(turn_cfg):
    from app.services.webrtc_manager.ice import get_ice_servers
    servers = get_ice_servers()
    assert [s.urls for s in servers] == [["stun:turn.example.org:3478"], ["turn:turn.example.org:3478"]]


# --- address classification ------------------------------------------------------------

@pytest.mark.parametrize("addr", [
    "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255", "192.168.8.100", "127.0.0.1", "127.9.9.9",
    "169.254.1.1", "100.64.0.1", "100.127.255.255", "0.0.0.0", "::1", "::", "fc00::1", "fdff::1",
    "fe80::1", "fe80::1%eth0", "febf::1", "::ffff:10.1.2.3", "::ffff:192.168.0.1", "::ffff:127.0.0.1",
    "::ffff:100.64.0.1", "box.local", "garbage",
])
def test_private_addresses(addr):
    assert is_private_address(addr)


@pytest.mark.parametrize("addr", [
    "77.79.152.227", "8.8.8.8", "172.15.255.255", "172.32.0.1", "100.63.255.255", "100.128.0.1",
    "169.253.0.1", "192.167.1.1", "11.0.0.1", "2001:db8::1", "2a00:1450::1", "fe00::1", "::ffff:8.8.8.8",
])
def test_public_addresses(addr):
    assert not is_private_address(addr)


# --- SDP rewriting (synthetic) -------------------------------------------------------------

SYNTH = "\r\n".join([
    "v=0", "o=- 1 1 IN IP4 0.0.0.0", "s=-", "t=0 0",
    "m=video 40000 UDP/TLS/RTP/SAVPF 97",
    "c=IN IP4 192.168.8.100",
    "a=rtcp:9 IN IP4 0.0.0.0",
    "a=candidate:f1 1 udp 2130706431 192.168.8.100 40000 typ host",
    "a=candidate:f2 1 udp 2130706431 10.1.1.1 40001 typ host",
    "a=candidate:f3 1 udp 2130706431 fd00::5 40002 typ host",
    "a=candidate:f4 1 udp 2130706431 ::ffff:172.16.0.9 40003 typ host",
    "a=candidate:f5 1 udp 2130706431 100.64.3.3 40004 typ host",
    "a=candidate:f6 1 udp 1694498815 203.0.113.5 40000 typ srflx raddr 192.168.8.100 rport 40000",
    "a=candidate:f7 1 udp 16777215 198.51.100.9 50000 typ relay raddr 10.0.0.2 rport 50001",
    "a=candidate:f8 1 udp 1 fe80::1%eth0 40005 typ host",
    "a=end-of-candidates",
    "",
])


def _addresses(text):
    return re.findall(r"(?<![\w:.])(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?:%\w+)?)(?![\w:.])", text, re.I)


def test_sanitize_synthetic():
    out = sanitize_sdp(SYNTH)
    cands = [l for l in out.split("\r\n") if l.startswith("a=candidate:")]
    assert [c.split()[4] for c in cands] == ["203.0.113.5", "198.51.100.9"]
    assert cands[0].endswith("raddr 0.0.0.0 rport 0")
    assert cands[1].endswith("raddr 0.0.0.0 rport 0")
    assert "m=video 40000 " in out and "c=IN IP4 203.0.113.5" in out  # placeholder -> first kept candidate
    assert "a=end-of-candidates" in out and out.endswith("\r\n")
    for a in _addresses(out):
        if a != "0.0.0.0":
            assert not is_private_address(a), a


def test_sanitize_no_candidates_left():
    only_private = "\r\n".join(l for l in SYNTH.split("\r\n") if "203.0.113" not in l and "198.51" not in l)
    out = sanitize_sdp(only_private)
    assert "a=candidate" not in out
    assert "m=video 9 " in out and "c=IN IP4 0.0.0.0" in out
    assert "192.168" not in out and "10.1.1.1" not in out


def test_sanitize_disabled_is_identity():
    assert sanitize_sdp(SYNTH, hide_private=False) == SYNTH


def test_public_ip_must_be_public():
    # a private "public ip" is refused, nothing added
    out = sanitize_sdp(SYNTH, hide_private=True, public_ip="10.9.9.9", base_ip="192.168.8.100")
    assert "10.9.9.9" not in out


# --- real aiortc -------------------------------------------------------------------------

async def _real_offer(port_range=None):
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))  # no STUN/TURN: no network traffic
    pc.addTransceiver(VideoStreamTrack(), direction="sendonly")
    await pc.setLocalDescription(await pc.createOffer())
    sdp = pc.localDescription.sdp
    return pc, sdp


@pytest.fixture
def no_port_range():
    yield
    portrange.configure_port_range(0, 0)
    import asyncio
    aioice.ice.asyncio = asyncio


async def test_real_aiortc_sdp_has_no_private_addresses(monkeypatch):
    pc, raw = await _real_offer()
    try:
        monkeypatch.setattr(settings, "WEBRTC_HIDE_PRIVATE_CANDIDATES", True)
        monkeypatch.setattr(settings, "WEBRTC_PUBLIC_IP", "")
        out = sanitize_local_sdp(raw)
        for a in _addresses(out):
            if a != "0.0.0.0":
                assert not is_private_address(a), f"private address left in SDP: {a}"
        for line in out.split("\r\n"):
            if line.startswith("a=candidate:"):
                assert not is_private_address(line.split()[4])
        # the rest of the SDP is intact
        assert "a=ice-ufrag:" in out and "a=fingerprint:" in out and "m=video" in out
    finally:
        await pc.close()


async def test_real_aiortc_hiding_can_be_disabled(monkeypatch):
    pc, raw = await _real_offer()
    try:
        monkeypatch.setattr(settings, "WEBRTC_HIDE_PRIVATE_CANDIDATES", False)
        monkeypatch.setattr(settings, "WEBRTC_PUBLIC_IP", "")
        assert sanitize_local_sdp(raw) == raw
    finally:
        await pc.close()


async def test_port_range_and_public_candidate(monkeypatch, no_port_range):
    lo, hi = 49152, 65535
    monkeypatch.setattr(settings, "WEBRTC_UDP_PORT_MIN", lo)
    monkeypatch.setattr(settings, "WEBRTC_UDP_PORT_MAX", hi)
    assert portrange.configure_port_range()
    pc, raw = await _real_offer()
    try:
        host = [l.split() for l in raw.split("\r\n") if l.startswith("a=candidate:") and l.endswith("typ host")]
        assert host, "no local host candidates in this environment"
        assert all(lo <= int(c[5]) <= hi for c in host)  # every ICE socket is inside the open range

        base = host[0][4]
        monkeypatch.setattr(sdp_mod, "default_route_ip", lambda: base)
        monkeypatch.setattr(settings, "WEBRTC_PUBLIC_IP", "198.51.100.7")
        monkeypatch.setattr(settings, "WEBRTC_HIDE_PRIVATE_CANDIDATES", True)
        out = sanitize_local_sdp(raw)
        pub = [l for l in out.split("\r\n") if l.startswith("a=candidate:") and "198.51.100.7" in l]
        assert len(pub) == 1
        t = pub[0].split()
        assert t[4] == "198.51.100.7" and t[5] == host[0][5] and "typ srflx" in pub[0]
        assert pub[0].endswith("raddr 0.0.0.0 rport 0")
        assert "c=IN IP4 198.51.100.7" in out and f"m=video {host[0][5]} " in out
        for a in _addresses(out):
            if a != "0.0.0.0":
                assert not is_private_address(a), a
    finally:
        await pc.close()


async def test_public_candidate_skipped_without_port_range(monkeypatch, no_port_range):
    pc, raw = await _real_offer()
    try:
        monkeypatch.setattr(settings, "WEBRTC_UDP_PORT_MIN", 0)
        monkeypatch.setattr(settings, "WEBRTC_UDP_PORT_MAX", 0)
        monkeypatch.setattr(settings, "WEBRTC_PUBLIC_IP", "198.51.100.7")
        assert "198.51.100.7" not in sanitize_local_sdp(raw)
    finally:
        await pc.close()


async def test_port_range_proxy_scope(no_port_range):
    assert portrange.configure_port_range(50000, 50100)
    assert aioice.ice.asyncio.__class__.__name__ == "_AsyncioProxy"
    import asyncio
    import aiortc.rtcpeerconnection as rpc
    assert asyncio.get_event_loop().__class__.__name__ != "_RangedLoop"  # everything else untouched
    assert rpc.asyncio is asyncio
    assert not portrange.configure_port_range(0, 0)
    assert not portrange.configure_port_range(5, 1)
