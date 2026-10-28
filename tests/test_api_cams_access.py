import pytest

import time

from app.core.codes import (FORBIDDEN_RESOURCE, LOCKED, OBJECT_NOT_FOUND, PTZ_NOT_CONFIGURED, TOKEN_ERROR)
from app.core.security import create_access_token, create_session_token, create_share_token
from app.models.invitation import Invitation
from app.models.sharing import Sharing
from tests.conftest import bearer

NOT_FOUND = OBJECT_NOT_FOUND
UNAUTHORIZED = TOKEN_ERROR
NEW = {"protocol": "rtsp", "address": "10.0.0.9", "port": 554, "username": "u", "password": "secret"}


def req(key, obj, **options):
    body = {"data": {key: obj}}
    if options:
        body["options"] = options
    return body


async def invite(db, owner_cam, email, status="accepted"):
    now = int(time.time())
    async with db() as s:
        s.add(Invitation(device_type="cams", device_id=owner_cam, email=email, invited_by=1,
                         status=status, added_at=now))
        await s.commit()


def tok(uid, role="user"):
    return create_access_token(uid, 1, role)


async def two_users(mk_user):
    return await mk_user("owner@x.io"), await mk_user("other@x.io")


async def test_cams_require_auth(client):
    for h, code in (({}, "046"), (bearer("junk"), "149")):
        r = await client.get("/v1/cams", headers=h)
        assert r.json()["code"] == code
    r = await client.get("/v1/cams", headers=bearer(create_session_token(1, 1)))
    assert r.json()["code"] == UNAUTHORIZED


async def test_create_cam_and_owner_sees_full(client, mk_user):
    owner = await mk_user("o@x.io")
    r = await client.post("/v1/cams", json=req("cam", NEW), headers=bearer(tok(owner)))
    assert r.status_code == 201 and r.json()["code"] == "001"
    cam = r.json()["data"]["cam"]
    assert cam["password"] == "secret" and cam["owner"] == owner and "id" in cam
    r = await client.get(f"/v1/cams/{cam['id']}", headers=bearer(tok(owner)))
    d = r.json()["data"]["cam"]
    assert r.json()["code"] == "000" and d["password"] == "secret" and d["owner"] == owner


async def test_create_cam_validation_and_envelope_errors(client, mk_user):
    h = bearer(tok(await mk_user("o@x.io")))
    r = await client.post("/v1/cams", json=NEW, headers=h)  # not a Request object
    assert r.status_code == 422 and r.json()["code"] == "109"
    r = await client.post("/v1/cams", json=req("cam", {"protocol": "rtsp"}), headers=h)
    assert r.json()["code"] == "109" and "address" in r.json()["error"]["fields"]
    r = await client.post("/v1/cams", content=b"{", headers={**h, "content-type": "application/json"})
    assert r.json()["code"] == "117"
    r = await client.post("/v1/cams", content=b"x", headers={**h, "content-type": "text/plain"})
    assert r.json()["code"] == "107"
    r = await client.post("/v1/cams", headers=h)
    assert r.json()["code"] == "121"
    r = await client.post("/v1/cams/5", json=req("cam", NEW), headers=h)  # POST with id is not routed
    assert r.status_code == 405 and r.json()["code"] == "096"


async def test_other_user_cannot_see_cam(client, mk_user, mk_cam):
    owner, other = await two_users(mk_user)
    cid = await mk_cam(owner)
    r = await client.get(f"/v1/cams/{cid}", headers=bearer(tok(other)))
    assert r.status_code == 404 and r.json()["code"] == NOT_FOUND == "082"
    r = await client.get("/v1/cams", headers=bearer(tok(other)))
    assert r.json()["data"]["settings"]["total"] == 0 and r.json()["code"] == "012"
    assert r.json()["data"]["cams"] == []


async def test_admin_sees_all(client, mk_user, mk_cam):
    owner, _ = await two_users(mk_user)
    admin = await mk_user("adm@x.io", role="admin")
    cid = await mk_cam(owner)
    r = await client.get(f"/v1/cams/{cid}", headers=bearer(tok(admin, "admin")))
    assert r.json()["code"] == "000" and r.json()["data"]["cam"]["id"] == cid


async def test_invited_user_visibility_and_restricted_view(client, mk_user, mk_cam, db):
    owner, other = await two_users(mk_user)
    cid = await mk_cam(owner, enabled=1)
    h_other = bearer(tok(other))
    await invite(db, cid, "Other@x.io", status="pending")
    assert (await client.get(f"/v1/cams/{cid}", headers=h_other)).json()["code"] == NOT_FOUND
    async with db() as s:
        inv = (await s.execute(__import__("sqlalchemy").select(Invitation))).scalars().one()
        inv.status = "accepted"
        await s.commit()
    r = await client.get(f"/v1/cams/{cid}", headers=h_other)
    d = r.json()["data"]["cam"]
    assert r.json()["code"] == "000"
    assert "password" not in d and "username" not in d  # restricted view


async def test_invited_user_cannot_see_disabled_cam(client, mk_user, mk_cam, db):
    owner, other = await two_users(mk_user)
    cid = await mk_cam(owner, enabled=0)
    await invite(db, cid, "other@x.io")
    assert (await client.get(f"/v1/cams/{cid}", headers=bearer(tok(other)))).json()["code"] == NOT_FOUND


async def test_invited_user_cannot_modify_or_delete(client, mk_user, mk_cam, db):
    owner, other = await two_users(mk_user)
    cid = await mk_cam(owner)
    await invite(db, cid, "other@x.io")
    h_other = bearer(tok(other))
    r = await client.patch(f"/v1/cams/{cid}", headers=h_other, json=req("cam", {"name": "hacked"}))
    assert r.status_code == 403 and r.json()["code"] == FORBIDDEN_RESOURCE
    r = await client.delete(f"/v1/cams/{cid}", headers=h_other)
    assert r.json()["code"] == FORBIDDEN_RESOURCE


async def test_stranger_cannot_patch_or_delete(client, mk_user, mk_cam):
    owner, other = await two_users(mk_user)
    cid = await mk_cam(owner)
    assert (await client.patch(f"/v1/cams/{cid}", headers=bearer(tok(other)),
                               json=req("cam", {"name": "x"}))).json()["code"] == NOT_FOUND
    assert (await client.delete(f"/v1/cams/{cid}", headers=bearer(tok(other)))).json()["code"] == NOT_FOUND


async def test_owner_patch_and_delete(client, mk_user, mk_cam):
    owner = await mk_user("o@x.io")
    h = bearer(tok(owner))
    cid = await mk_cam(owner)
    r = await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {"name": "new"}))
    assert r.json()["code"] == "003" and r.json()["data"]["cam"]["name"] == "new"
    r = await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {"name": "new"}))
    assert r.json()["code"] == "005"  # nothing changed
    r = await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {"bogus": 1}))
    assert r.json()["code"] == "109"
    r = await client.delete(f"/v1/cams/{cid}", headers=h)
    assert r.json()["code"] == "007" and r.json()["data"] == {"cam": {"id": cid}}
    assert (await client.get(f"/v1/cams/{cid}", headers=h)).json()["code"] == NOT_FOUND


async def test_list_settings_search_sort_limits(client, mk_user, mk_cam):
    owner = await mk_user("o@x.io")
    h = bearer(tok(owner))
    for n in ("alpha", "beta", "gamma"):
        await client.post("/v1/cams", headers=h, json=req("cam", {**NEW, "name": n, "address": f"{n}.lan"}))
    r = await client.get("/v1/cams", headers=h, params={"limit": 2, "sort_by": "name", "order": "desc"})
    j = r.json()
    assert j["code"] == "000" and [c["name"] for c in j["data"]["cams"]] == ["gamma", "beta"]
    st = j["data"]["settings"]
    assert st["total"] == 3 and st["limit"] == 2 and st["offset"] == 0 and st["order"] == "descending"
    assert j["metadata"]["page"] == {"current": 1, "pages": 2}
    # multi search: name include "al" OR name include "ga"
    r = await client.get("/v1/cams", headers=h, params={"search": "al,ga", "search_column": "name,name"})
    assert sorted(c["name"] for c in r.json()["data"]["cams"]) == ["alpha", "gamma"]
    r = await client.get("/v1/cams", headers=h, params={"search": "beta.lan", "search_column": "address",
                                                        "search_method": "exactly"})
    assert [c["name"] for c in r.json()["data"]["cams"]] == ["beta"]
    assert r.json()["data"]["settings"]["search_method"] == "exactly"
    # errors
    assert (await client.get("/v1/cams", headers=h, params={"limit": 999})).json()["code"] == "183"
    assert (await client.get("/v1/cams", headers=h, params={"limit": "x"})).json()["code"] == "145"
    assert (await client.get("/v1/cams", headers=h, params={"sort_by": "password"})).json()["code"] == "143"
    assert (await client.get("/v1/cams", headers=h, params={"search": "a", "search_column": "password"})
            ).json()["code"] == "144"


async def test_options_supported_functions(client):
    r = await client.options("/v1/cams")
    fns = r.headers["cursed-supported-functions"].split(",")
    for f in ("limit", "offset", "sort_by", "order", "search", "multi_search", "pagination"):
        assert f in fns
    r = await client.options("/v1/cams/bulk")
    fns = r.headers["cursed-supported-functions"].split(",")
    assert "bulk" in fns and "partial" in fns
    r = await client.options("/v1/cams/1/recordings")
    assert "x-time_range" in r.headers["cursed-supported-functions"].split(",")


# --- bulk ---------------------------------------------------------------------
async def test_bulk_create_atomic_and_partial(client, mk_user):
    h = bearer(tok(await mk_user("o@x.io")))
    good = {**NEW, "name": "a"}
    bad = {"protocol": "rtsp"}
    r = await client.post("/v1/cams/bulk", headers=h, json={"data": {"cams": [good, bad]}})
    assert r.status_code == 422 and r.json()["code"] == "109"
    assert (await client.get("/v1/cams", headers=h)).json()["data"]["settings"]["total"] == 0  # nothing applied
    r = await client.post("/v1/cams/bulk", headers=h, json={"data": {"cams": [good, {**good, "name": "b"}]}})
    assert r.status_code == 201 and r.json()["code"] == "001" and len(r.json()["data"]["cams"]) == 2
    r = await client.post("/v1/cams/bulk", headers=h, json={"data": {"cams": [good, bad]},
                                                            "options": {"partial_allowed": True}})
    j = r.json()
    assert j["code"] == "002"
    assert len(j["data"]["cams"]["succeeded"]) == 1 and j["data"]["cams"]["failed"][0]["id"] == 1
    assert "address" in j["data"]["cams"]["failed"][0]["error"]["fields"]
    r = await client.post("/v1/cams/bulk?partial_allowed=true", headers=h, json={"data": {"cams": [bad]}})
    assert r.json()["code"] == "109" and r.json()["data"]["cams"]["succeeded"] == []  # all failed
    assert (await client.post("/v1/cams/bulk", headers=h, json={"data": {"cams": [good] * 101}})
            ).json()["code"] == "146"


async def test_bulk_get_patch_delete(client, mk_user, mk_cam):
    owner, other = await two_users(mk_user)
    h = bearer(tok(owner))
    c1, c2 = await mk_cam(owner), await mk_cam(owner)
    foreign = await mk_cam(other)
    r = await client.get(f"/v1/cams/bulk/{c1},{c2}", headers=h)
    j = r.json()
    assert j["code"] == "000" and [c["id"] for c in j["data"]["cams"]] == [c1, c2]
    assert j["data"]["settings"]["total"] == 2
    # a foreign id and a non-existent id are indistinguishable: atomic 082, nothing returned
    for ids in (f"{c1},{c2},{foreign}", f"{c1},{c2},99999", f"{foreign}"):
        r = await client.get(f"/v1/cams/bulk/{ids}", headers=h)
        assert r.status_code == 404 and r.json()["code"] == "082", ids
        assert "cams" not in (r.json().get("data") or {})
    # partial_allowed is not a thing for GET: still 082
    r = await client.get(f"/v1/cams/bulk/{c1},{foreign}?partial_allowed=true", headers=h)
    assert r.json()["code"] != "000"
    # admin sees everything
    ra = await client.get(f"/v1/cams/bulk/{c1},{foreign}", headers=bearer(tok(owner, "admin")))
    assert ra.json()["code"] == "000" and len(ra.json()["data"]["cams"]) == 2
    assert (await client.get("/v1/cams/bulk/1,x", headers=h)).json()["code"] == "109"

    patch = {"data": {"cams": [{"name": "n1"}, {"name": "n2"}]}}
    r = await client.patch(f"/v1/cams/bulk/{c1},{c2}", headers=h, json=patch)
    assert r.json()["code"] == "003" and [c["name"] for c in r.json()["data"]["cams"]] == ["n1", "n2"]
    # length mismatch
    assert (await client.patch(f"/v1/cams/bulk/{c1}", headers=h, json=patch)).json()["code"] == "109"
    # atomic: foreign id rejects everything
    r = await client.patch(f"/v1/cams/bulk/{c1},{foreign}", headers=h, json={"data": {"cams": [{"name": "z"}] * 2}})
    assert r.status_code == 404 and r.json()["code"] == "082"
    assert (await client.get(f"/v1/cams/{c1}", headers=h)).json()["data"]["cam"]["name"] == "n1"
    # partial
    r = await client.patch(f"/v1/cams/bulk/{c1},{foreign}", headers=h,
                           json={"data": {"cams": [{"name": "z"}] * 2}, "options": {"partial_allowed": True}})
    j = r.json()
    assert j["code"] == "004" and j["data"]["cams"]["failed"][0]["id"] == foreign
    assert j["data"]["cams"]["succeeded"][0]["name"] == "z"

    r = await client.delete(f"/v1/cams/bulk/{c1},{foreign}", headers=h)
    assert r.json()["code"] == "082"
    assert (await client.get(f"/v1/cams/{c1}", headers=h)).json()["code"] == "000"
    r = await client.delete(f"/v1/cams/bulk/{c1},{foreign}?partial_allowed=true", headers=h)
    assert r.json()["code"] == "008" and r.json()["data"]["cams"]["succeeded"] == [{"id": c1}]
    r = await client.delete(f"/v1/cams/bulk/{c2}", headers=h)
    assert r.json()["code"] == "007"
    assert (await client.get(f"/v1/cams/{c2}", headers=h)).json()["code"] == "082"


# --- PTZ / onvif / recordings -------------------------------------------------
async def test_ptz_request_object_and_lock(client, mk_user, mk_cam, monkeypatch):
    owner = await mk_user("o@x.io")
    h = bearer(tok(owner))
    cid = await mk_cam(owner, control_proto="onvif")
    moved = []

    async def fake_move(cam, pan, tilt, zoom):
        moved.append((pan, tilt, zoom))

    async def fake_status(cam):
        return {"pan": 0.1, "tilt": 0.2, "zoom": 0.3}

    monkeypatch.setattr("app.routers.cams.stream_ptz.onvif_client.absolute_move", fake_move)
    monkeypatch.setattr("app.routers.cams.stream_ptz.onvif_client.get_status", fake_status)
    r = await client.get(f"/v1/cams/{cid}/ptz", headers=h)
    assert r.json()["data"]["ptz"] == {"pan": 0.1, "tilt": 0.2, "zoom": 0.3}
    assert (await client.post(f"/v1/cams/{cid}/ptz", headers=h, json={"pan": 1})).json()["code"] == "109"
    assert (await client.post(f"/v1/cams/{cid}/ptz", headers=h, json=req("ptz", {}))).json()["code"] == "109"
    r = await client.post(f"/v1/cams/{cid}/ptz", headers=h, json=req("ptz", {"pan": 0.5}))
    assert r.json()["code"] == "000" and r.json()["data"]["ptz"]["id"] == cid and moved == [(0.5, None, None)]
    r = await client.post(f"/v1/cams/{cid}/ptz", headers=h, json=req("ptz", {"pan": 0.6}))
    assert r.status_code == 423 and r.json()["code"] == LOCKED == "110"


async def test_ptz_not_configured_code(client, mk_user, mk_cam):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    r = await client.post(f"/v1/cams/{cid}/ptz", headers=bearer(tok(owner)), json=req("ptz", {"pan": 1}))
    assert r.status_code == 409 and r.json()["code"] == PTZ_NOT_CONFIGURED == "176"


async def test_onvif_patch(client, mk_user, mk_cam):
    owner, other = await two_users(mk_user)
    cid = await mk_cam(owner)
    r = await client.patch(f"/v1/cams/{cid}/onvif", headers=bearer(tok(owner)),
                           json=req("onvif", {"onvif_enabled": 1}))
    assert r.json()["code"] == "003" and r.json()["data"]["onvif"]["onvif_enabled"] == 1
    r = await client.patch(f"/v1/cams/{cid}/onvif", headers=bearer(tok(other)),
                           json=req("onvif", {"onvif_enabled": 0}))
    assert r.json()["code"] == FORBIDDEN_RESOURCE
    r = await client.patch(f"/v1/cams/{cid}/onvif", headers=bearer(tok(owner)),
                           json=req("onvif", {"onvif_enabled": 5}))
    assert r.json()["code"] == "109"


async def test_recording_config_and_list(client, mk_user, mk_cam, db, tmp_path):
    from app.models.user import User
    from sqlalchemy import update
    owner = await mk_user("o@x.io")
    h = bearer(tok(owner))
    cid = await mk_cam(owner)
    assert (await client.get(f"/v1/cams/{cid}/recording", headers=h)).json()["data"]["recording_config"]["record"] == 0
    cfg = req("recording_config", {"record": 1, "record_type": "local", "record_metadata": {"local": {"path": str(tmp_path)}}})
    assert (await client.patch(f"/v1/cams/{cid}/recording", headers=h, json=cfg)).json()["code"] == "062"
    async with db() as s:
        await s.execute(update(User).where(User.id == owner).values(allowrecord=1))
        await s.commit()
    r = await client.patch(f"/v1/cams/{cid}/recording", headers=h, json=req("recording_config", {"record": 1}))
    assert r.json()["code"] == "109" and "record_type" in r.json()["error"]["fields"]
    r = await client.patch(f"/v1/cams/{cid}/recording", headers=h, json=cfg)
    assert r.json()["code"] == "003" and r.json()["data"]["recording_config"]["record_type"] == "local"

    import os
    for i, name in enumerate(("a.mp4", "b.mp4", "c.mp4")):
        f = tmp_path / name
        f.write_bytes(b"x" * (i + 1))
        os.utime(f, (1000 + i * 10, 1000 + i * 10))
    (tmp_path / "skip.txt").write_text("no")
    r = await client.get(f"/v1/cams/{cid}/recordings", headers=h)
    j = r.json()
    assert j["code"] == "000" and [x["id"] for x in j["data"]["recordings"]] == ["c.mp4", "b.mp4", "a.mp4"]
    assert j["data"]["settings"]["total"] == 3 and j["data"]["settings"]["limit"] == 10
    r = await client.get(f"/v1/cams/{cid}/recordings", headers=h,
                         params={"sort_by": "size_bytes", "order": "asc", "limit": 2, "offset": 1})
    assert [x["id"] for x in r.json()["data"]["recordings"]] == ["b.mp4", "c.mp4"]
    r = await client.get(f"/v1/cams/{cid}/recordings", headers=h, params={"search": "b", "search_column": "filename"})
    assert [x["id"] for x in r.json()["data"]["recordings"]] == ["b.mp4"]
    r = await client.get(f"/v1/cams/{cid}/recordings", headers=h, params={"from": 1010, "to": 1015})
    assert [x["id"] for x in r.json()["data"]["recordings"]] == ["b.mp4"]
    assert (await client.get(f"/v1/cams/{cid}/recordings", headers=h, params={"from": "x"})).json()["code"] == "118"
    assert (await client.get(f"/v1/cams/{cid}/recordings", headers=h, params={"limit": 101})).json()["code"] == "183"
    r = await client.get(f"/v1/cams/{cid}/recordings/a.mp4", headers=h)
    assert r.json()["data"]["recording"]["size_bytes"] == 1
    r = await client.get(f"/v1/cams/{cid}/recordings/nope.mp4", headers=h)
    assert r.status_code == 404 and r.json()["code"] == "082"
    r = await client.get(f"/v1/cams/{cid}/recordings/a.mp4/file", headers=h)
    assert r.status_code == 200 and r.content == b"x"  # binary untouched


async def test_forged_admin_role_via_user_secret_is_not_admin(client, mk_user, mk_cam):
    """role claim signed with the ordinary access secret must not grant admin visibility.
    (create_access_token picks the secret by role, so craft by hand.)"""
    import time, jwt
    from app.core.config import settings
    owner, other = await two_users(mk_user)
    cid = await mk_cam(owner)
    now = int(time.time())
    forged = jwt.encode({"uid": other, "sid": 1, "role": "admin", "iat": now, "exp": now + 99},
                        settings.JWT_SECRET_ACCESS, algorithm="HS256")
    r = await client.get(f"/v1/cams/{cid}", headers=bearer(forged))
    assert r.json()["code"] == NOT_FOUND


# --- share-token gated endpoint: GET /v1/cams/{id}/ptz (control_proto None -> 176 once auth passes) ---
_DB = {}


@pytest.fixture(autouse=True)
def _grab_db(db):
    _DB["sm"] = db


async def _share(client, owner, cid, typ="stream", ttl=120, **extra):
    now = int(time.time())
    async with _DB["sm"]() as s:
        sh = Sharing(uid=owner, type=typ, device_type="cams", device_id=cid,
                     filename=extra.get("filename"), added_at=now, updated_at=now)
        s.add(sh)
        await s.commit()
        sid = sh.id
    tkn = create_share_token(sid, owner, typ, "cams", cid, extra.get("filename"), ttl)
    return {"id": sid, "token": tkn}


async def test_stream_share_token_grants_access_to_that_cam(client, mk_user, mk_cam):
    owner, _ = await two_users(mk_user)
    cid = await mk_cam(owner)
    share = await _share(client, owner, cid)
    r = await client.get(f"/v1/cams/{cid}/ptz", headers=bearer(share["token"]))
    assert r.json()["code"] == PTZ_NOT_CONFIGURED  # passed auth
    # The client dash-encodes query tokens (a '-' after every 10 chars, see _decode_dashed);
    # a raw JWT here would flake whenever a literal base64url '-' lands on a chunk boundary.
    tkn = share["token"]
    dashed = "-".join(tkn[i:i + 10] for i in range(0, len(tkn), 10))
    r = await client.get(f"/v1/cams/{cid}/ptz", params={"token": dashed})
    assert r.json()["code"] == PTZ_NOT_CONFIGURED  # via query too


async def test_stream_share_for_other_cam_rejected(client, mk_user, mk_cam):
    owner, _ = await two_users(mk_user)
    c1, c2 = await mk_cam(owner), await mk_cam(owner)
    share = await _share(client, owner, c1)
    r = await client.get(f"/v1/cams/{c2}/ptz", headers=bearer(share["token"]))
    assert r.json()["code"] == UNAUTHORIZED


async def test_record_share_cannot_be_used_for_stream(client, mk_user, mk_cam, monkeypatch):
    owner, _ = await two_users(mk_user)
    cid = await mk_cam(owner)
    share = await _share(client, owner, cid, typ="record", filename="a.mp4")
    r = await client.get(f"/v1/cams/{cid}/ptz", headers=bearer(share["token"]))
    assert r.json()["code"] == UNAUTHORIZED


async def test_revoked_share_rejected(client, mk_user, mk_cam):
    owner, _ = await two_users(mk_user)
    cid = await mk_cam(owner)
    share = await _share(client, owner, cid)
    async with _DB["sm"]() as s:
        await s.delete(await s.get(Sharing, share["id"]))
        await s.commit()
    r = await client.get(f"/v1/cams/{cid}/ptz", headers=bearer(share["token"]))
    assert r.json()["code"] == UNAUTHORIZED


async def test_expired_share_rejected(client, mk_user, mk_cam, db):
    from app.core.security import create_share_token
    from app.models.sharing import Sharing
    import time
    owner, _ = await two_users(mk_user)
    cid = await mk_cam(owner)
    async with db() as s:
        sh = Sharing(uid=owner, type="stream", device_type="cams", device_id=cid,
                     added_at=int(time.time()), updated_at=int(time.time()))
        s.add(sh)
        await s.commit()
        sid = sh.id
    expired = create_share_token(sid, owner, "stream", "cams", cid, None, -10)
    r = await client.get(f"/v1/cams/{cid}/ptz", headers=bearer(expired))
    assert r.json()["code"] == "043"
    valid = create_share_token(sid, owner, "stream", "cams", cid, None, 100)
    r = await client.get(f"/v1/cams/{cid}/ptz", headers=bearer(valid))
    assert r.json()["code"] == PTZ_NOT_CONFIGURED


async def test_share_token_signed_with_foreign_secret_rejected(client, mk_user, mk_cam):
    import time, jwt
    owner, _ = await two_users(mk_user)
    cid = await mk_cam(owner)
    now = int(time.time())
    forged = jwt.encode({"sha_id": 1, "user_id": owner, "type": "stream", "device_type": "cams",
                         "device_id": cid, "filename": None, "iat": now, "exp": now + 99},
                        "attacker-secret-attacker-secret-1234", algorithm="HS256")
    r = await client.get(f"/v1/cams/{cid}/ptz", headers=bearer(forged))
    assert r.json()["code"] == "044"
