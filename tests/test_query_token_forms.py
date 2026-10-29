"""?token= accepts both the plain token and the dash-encoded one (a '-' after every
10 chars) on every query-token endpoint, with identical results."""
import time

import pytest

from app.core.codes import PTZ_NOT_CONFIGURED, TOKEN_ERROR
from app.core.errors import AppError
from app.core.security import create_access_token, create_share_token
from app.deps import auth as deps
from app.models.sharing import Sharing
from tests.conftest import bearer
from tests.test_webrtc_ws import fake_rtc, ws_connect  # noqa: F401


def dash(tok, n=10):
    return "-".join(tok[i:i + n] for i in range(0, len(tok), n))


def _token_with_literal_dash_at_chunk_boundary():
    """An access token whose plain form is mangled by a blind _decode_dashed."""
    for uid in range(1, 20000):
        t = create_access_token(uid, 1, "user")
        if deps._decode_dashed(t) != t:
            return t
    pytest.skip("no suitable token found")


async def _mk_share(sm, owner, cid, typ, device_type="cams", filename=None, ttl=300):
    now = int(time.time())
    async with sm() as s:
        row = Sharing(uid=owner, type=typ, device_type=device_type, device_id=cid,
                      filename=filename, added_at=now, updated_at=now)
        s.add(row)
        await s.commit()
        sid = row.id
    return create_share_token(sid, owner, typ, device_type, cid, filename, ttl)


# --- normalising helper -------------------------------------------------------

def test_flexible_plain_and_dashed_equal():
    t = create_access_token(1, 1, "user")
    assert deps.extract_token_flexible(None, t) == t
    assert deps.extract_token_flexible(None, dash(t)) == t


def test_flexible_literal_dash_in_plain_token_survives():
    t = _token_with_literal_dash_at_chunk_boundary()
    assert "-" in t
    assert deps.extract_token_flexible(None, t) == t          # plain, not mangled
    assert deps.extract_token_flexible(None, dash(t)) == t    # dashed


def test_flexible_invalid_keeps_previous_behaviour():
    assert deps.extract_token_flexible(None, "garbage") == "garbage"
    # unverifiable dashed input -> de-dashed reading, same as before the change
    assert deps.extract_token_flexible(None, "abcdefghij-klmno") == "abcdefghijklmno"
    with pytest.raises(AppError):
        deps.extract_token_flexible(None, None)
    with pytest.raises(AppError):
        deps.extract_token_flexible("Basic x", "")


def test_flexible_header_priority():
    good = create_access_token(1, 1, "user")
    other = create_access_token(2, 1, "user")
    assert deps.extract_token_flexible(f"Bearer {good}", dash(other)) == good
    assert deps.extract_token_flexible(f"Bearer {good}", other) == good
    assert deps.extract_token_flexible("Bearer ", dash(other)) == other


@pytest.mark.parametrize("form", [lambda t: t, dash])
def test_require_access_qs_both_forms(form):
    t = create_access_token(7, 1, "user")
    tok = deps.get_bearer_token_or_query(None, form(t))
    assert tok == t
    assert deps.require_access_qs(tok).kind == "access"


# --- resolve_stream_access: GET /v1/cams/{id}/ptz -------------------------------

async def test_stream_endpoint_plain_and_dashed_same(client, mk_user, mk_cam, db):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    t = await _mk_share(db, owner, cid, "stream")
    r1 = await client.get(f"/v1/cams/{cid}/ptz", params={"token": t})
    r2 = await client.get(f"/v1/cams/{cid}/ptz", params={"token": dash(t)})
    assert r1.json()["code"] == r2.json()["code"] == PTZ_NOT_CONFIGURED


async def test_stream_endpoint_access_token_forms(client, mk_user, mk_cam):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    t = create_access_token(owner, 1, "user")
    for q in (t, dash(t)):
        r = await client.get(f"/v1/cams/{cid}/ptz", params={"token": q})
        assert r.json()["code"] == PTZ_NOT_CONFIGURED


async def test_stream_endpoint_bad_token_same_error(client, mk_user, mk_cam, db):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    t = await _mk_share(db, owner, cid, "stream")
    bad = t[:-3] + ("AAA" if not t.endswith("AAA") else "BBB")
    codes = set()
    for q in (bad, dash(bad), "garbage", dash("garbage" * 4)):
        r = await client.get(f"/v1/cams/{cid}/ptz", params={"token": q})
        codes.add(r.json()["code"])
    assert PTZ_NOT_CONFIGURED not in codes and len(codes) <= 2


async def test_stream_endpoint_header_priority(client, mk_user, mk_cam, db):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    good = await _mk_share(db, owner, cid, "stream")
    # valid header + garbage query -> header wins
    r = await client.get(f"/v1/cams/{cid}/ptz", headers=bearer(good), params={"token": "garbage"})
    assert r.json()["code"] == PTZ_NOT_CONFIGURED
    # bad header + valid query -> header wins, so it fails
    r = await client.get(f"/v1/cams/{cid}/ptz", headers=bearer("garbage"), params={"token": good})
    assert r.json()["code"] != PTZ_NOT_CONFIGURED


async def test_stream_endpoint_wrong_cam_rejected_in_both_forms(client, mk_user, mk_cam, db):
    owner = await mk_user("o@x.io")
    c1, c2 = await mk_cam(owner), await mk_cam(owner)
    t = await _mk_share(db, owner, c1, "stream")
    for q in (t, dash(t)):
        r = await client.get(f"/v1/cams/{c2}/ptz", params={"token": q})
        assert r.json()["code"] == TOKEN_ERROR


# --- resolvers called directly (record, device_use) ---------------------------

async def test_record_resolver_both_forms(mk_user, mk_cam, db):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    t = await _mk_share(db, owner, cid, "record", filename="a.mp4")
    async with db() as s:
        for q in (t, dash(t)):
            ctx = await deps.resolve_record_access(cid, "a.mp4", None, q, s)
            assert ctx.share is not None and ctx.share.filename == "a.mp4"
            with pytest.raises(AppError) as e:
                await deps.resolve_record_access(cid, "other.mp4", None, q, s)
            assert e.value.code == TOKEN_ERROR
        with pytest.raises(AppError) as e:
            await deps.resolve_record_access(cid, "a.mp4", None, "garbage", s)
        # header priority
        acc = create_access_token(owner, 1, "user")
        ctx = await deps.resolve_record_access(cid, "a.mp4", f"Bearer {acc}", "garbage", s)
        assert ctx.payload is not None


async def test_record_resolver_literal_dash_access_token(mk_user, db):
    t = _token_with_literal_dash_at_chunk_boundary()
    async with db() as s:
        for q in (t, dash(t)):
            ctx = await deps.resolve_record_access(1, "a.mp4", None, q, s)
            assert ctx.payload is not None


async def test_device_use_resolver_both_forms(mk_user, db):
    owner = await mk_user("o@x.io")
    t = await _mk_share(db, owner, 5, "device_use", device_type="switches")
    async with db() as s:
        for q in (t, dash(t)):
            ctx = await deps.resolve_device_use_access(5, None, q, s)
            assert ctx.share is not None
            with pytest.raises(AppError) as e:
                await deps.resolve_device_use_access(6, None, q, s)
            assert e.value.code == TOKEN_ERROR
        with pytest.raises(AppError):
            await deps.resolve_device_use_access(5, None, "garbage", s)
        acc = create_access_token(owner, 1, "user")
        ctx = await deps.resolve_device_use_access(5, f"Bearer {acc}", "garbage", s)
        assert ctx.payload is not None


# --- websocket /v1/cams/{id}/webrtc ----------------------------------------------

async def test_webrtc_ws_plain_and_dashed(app, mk_user, mk_cam, fake_rtc):  # noqa: F811
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    t = create_access_token(owner, 1, "user")
    for q in (t, dash(t)):
        ok, code, _ = await ws_connect(app, cid, q)
        assert ok
    ok, code, _ = await ws_connect(app, cid, "garbage")
    assert not ok and code == 1008
