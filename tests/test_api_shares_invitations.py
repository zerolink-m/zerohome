import time

import pytest

from app.core.codes import (FORBIDDEN_RESOURCE, INVITATION_INVALID, OBJECT_NOT_FOUND as NOT_FOUND, SHARE_INVALID)
from app.core.security import create_access_token, decode_share_token
from tests.conftest import bearer


def tok(uid, role="user"):
    return create_access_token(uid, 1, role)


@pytest.fixture
async def setup(mk_user, mk_cam):
    owner = await mk_user("owner@x.io")
    other = await mk_user("other@x.io")
    admin = await mk_user("adm@x.io", role="admin")
    cam = await mk_cam(owner)
    return owner, other, admin, cam


def share_body(cam, **kw):
    return {"data": {"share": {"device_type": "cams", "device_id": cam, "type": "stream", **kw}}}


def _raw(device_type, device_id, type_):
    return {"data": {"share": {"device_type": device_type, "device_id": device_id, "type": type_}}}


# ---------------- shares ----------------
async def test_create_share_default_ttl_and_token_contents(client, setup):
    owner, _, _, cam = setup
    r = await client.post("/v1/shares", headers=bearer(tok(owner)), json=share_body(cam))
    assert r.status_code == 201 and r.json()["code"] == "001"
    d = r.json()["data"]["share"]
    p = decode_share_token(d["token"])
    assert (p.sha_id, p.user_id, p.type, p.device_type, p.device_id) == (d["id"], owner, "stream", "cams", cam)
    assert p.exp - p.iat == 60  # SHARE_TOKEN_MIN_TTL_SECONDS in tests
    assert d["expires_at"] == p.exp or abs(d["expires_at"] - p.exp) <= 1


@pytest.mark.parametrize("ttl", [59, 3601])
async def test_share_ttl_out_of_range(client, setup, ttl):
    owner, _, _, cam = setup
    r = await client.post("/v1/shares", headers=bearer(tok(owner)), json=share_body(cam, ttl_seconds=ttl))
    assert r.json()["code"] == SHARE_INVALID


@pytest.mark.parametrize("ttl", [60, 3600])
async def test_share_ttl_boundaries_ok(client, setup, ttl):
    owner, _, _, cam = setup
    r = await client.post("/v1/shares", headers=bearer(tok(owner)), json=share_body(cam, ttl_seconds=ttl))
    assert r.json()["code"] == "001"


async def test_share_invalid_type_for_device(client, setup):
    owner, _, _, cam = setup
    r = await client.post("/v1/shares", headers=bearer(tok(owner)),
                          json=_raw("cams", cam, "device_use"))
    assert r.json()["code"] == SHARE_INVALID
    r = await client.post("/v1/shares", headers=bearer(tok(owner)),
                          json=_raw("switches", 1, "stream"))
    assert r.json()["code"] == SHARE_INVALID


async def test_share_validation_filename_rules(client, setup):
    owner, _, _, cam = setup
    r = await client.post("/v1/shares", headers=bearer(tok(owner)), json=share_body(cam, filename="x.mp4"))
    assert r.json()["code"] == "109"
    r = await client.post("/v1/shares", headers=bearer(tok(owner)), json=share_body(cam, type="record"))
    assert r.json()["code"] == "109"


async def test_share_only_owner_or_admin(client, setup):
    owner, other, admin, cam = setup
    r = await client.post("/v1/shares", headers=bearer(tok(other)), json=share_body(cam))
    assert r.json()["code"] == FORBIDDEN_RESOURCE
    r = await client.post("/v1/shares", headers=bearer(tok(admin, "admin")), json=share_body(cam))
    assert r.json()["code"] == "001"


async def test_share_unknown_device(client, setup):
    owner, *_ = setup
    r = await client.post("/v1/shares", headers=bearer(tok(owner)), json=share_body(9999))
    assert r.json()["code"] == NOT_FOUND


async def test_share_requires_auth(client, setup):
    _, _, _, cam = setup
    r = await client.post("/v1/shares", json=share_body(cam))
    assert r.json()["code"] == "046"


async def test_share_list_get_delete_scoping(client, setup):
    owner, other, admin, cam = setup
    sid = (await client.post("/v1/shares", headers=bearer(tok(owner)), json=share_body(cam))).json()["data"]["share"]["id"]
    assert (await client.get("/v1/shares", headers=bearer(tok(owner)))).json()["data"]["settings"]["total"] == 1
    assert (await client.get("/v1/shares", headers=bearer(tok(other)))).json()["data"]["settings"]["total"] == 0
    assert (await client.get("/v1/shares", headers=bearer(tok(admin, "admin")))).json()["data"]["settings"]["total"] == 1
    assert (await client.get(f"/v1/shares/{sid}", headers=bearer(tok(other)))).json()["code"] == NOT_FOUND
    assert (await client.delete(f"/v1/shares/{sid}", headers=bearer(tok(other)))).json()["code"] == NOT_FOUND
    assert (await client.delete(f"/v1/shares/{sid}", headers=bearer(tok(owner)))).json()["code"] == "007"
    assert (await client.get(f"/v1/shares/{sid}", headers=bearer(tok(owner)))).json()["code"] == NOT_FOUND


async def test_share_token_itself_cannot_call_shares_api(client, setup):
    owner, _, _, cam = setup
    t = (await client.post("/v1/shares", headers=bearer(tok(owner)), json=share_body(cam))).json()["data"]["share"]["token"]
    assert (await client.get("/v1/shares", headers=bearer(t))).json()["code"] == "044"


# ---------------- invitations ----------------
def inv_body(cam, email="other@x.io"):
    return {"data": {"invitation": {"device_type": "cams", "device_id": cam, "email": email}}}


async def test_invitation_create_rules(client, setup):
    owner, other, admin, cam = setup
    r = await client.post("/v1/invitations", headers=bearer(tok(owner)), json=inv_body(cam))
    assert r.status_code == 201 and r.json()["code"] == "001"
    # duplicate (case-insensitive)
    r = await client.post("/v1/invitations", headers=bearer(tok(owner)), json=inv_body(cam, "OTHER@x.io"))
    assert r.json()["code"] == INVITATION_INVALID
    # inviting the owner
    r = await client.post("/v1/invitations", headers=bearer(tok(owner)), json=inv_body(cam, "owner@x.io"))
    assert r.json()["code"] == INVITATION_INVALID
    # non-owner
    r = await client.post("/v1/invitations", headers=bearer(tok(other)), json=inv_body(cam, "z@x.io"))
    assert r.json()["code"] == FORBIDDEN_RESOURCE
    # unknown device
    r = await client.post("/v1/invitations", headers=bearer(tok(owner)), json=inv_body(9999))
    assert r.json()["code"] == NOT_FOUND


async def test_invitation_visibility_and_patch_rights(client, setup, mk_user):
    owner, other, admin, cam = setup
    third = await mk_user("third@x.io")
    iid = (await client.post("/v1/invitations", headers=bearer(tok(owner)), json=inv_body(cam))).json()["data"]["invitation"]["id"]
    # third party sees nothing
    assert (await client.get(f"/v1/invitations/{iid}", headers=bearer(tok(third)))).json()["code"] == NOT_FOUND
    assert (await client.get("/v1/invitations", headers=bearer(tok(third)))).json()["data"]["settings"]["total"] == 0
    # sender and recipient see it
    assert (await client.get(f"/v1/invitations/{iid}", headers=bearer(tok(owner)))).json()["code"] == "000"
    assert (await client.get(f"/v1/invitations/{iid}", headers=bearer(tok(other)))).json()["code"] == "000"
    # sender cannot accept on behalf of the invitee
    r = await client.post(f"/v1/invitations/{iid}/accept", headers=bearer(tok(owner)))
    assert r.json()["code"] == FORBIDDEN_RESOURCE
    r = await client.post(f"/v1/invitations/{iid}/accept", headers=bearer(tok(other)))
    assert r.json()["code"] == "003"
    assert r.json()["data"]["invitation"]["status"] == "accepted"
    # repeated accept: not changed
    r = await client.post(f"/v1/invitations/{iid}/accept", headers=bearer(tok(other)))
    assert r.json()["code"] == "005"
    got = (await client.get(f"/v1/invitations/{iid}", headers=bearer(tok(other)))).json()["data"]["invitation"]
    assert got["status"] == "accepted" and got["updated_at"] is not None
    # PATCH on an invitation no longer exists
    r = await client.patch(f"/v1/invitations/{iid}", headers=bearer(tok(other)), json={"data": {"invitation": {}}})
    assert r.status_code == 405


async def test_invitation_decline(client, setup):
    owner, other, admin, cam = setup
    iid = (await client.post("/v1/invitations", headers=bearer(tok(owner)), json=inv_body(cam))).json()["data"]["invitation"]["id"]
    assert (await client.post(f"/v1/invitations/{iid}/decline", headers=bearer(tok(owner)))).json()["code"] == FORBIDDEN_RESOURCE
    r = await client.post(f"/v1/invitations/{iid}/decline", headers=bearer(tok(other)))
    assert r.json()["code"] == "007"
    assert (await client.get(f"/v1/invitations/{iid}", headers=bearer(tok(owner)))).json()["code"] == NOT_FOUND


async def test_invitation_body_validation(client, setup):
    owner, _, _, cam = setup
    r = await client.post("/v1/invitations", headers=bearer(tok(owner)), json={"device_type": "cams"})
    assert r.json()["code"] == "109"
    r = await client.post("/v1/invitations", headers=bearer(tok(owner)),
                          json={"data": {"invitation": {"device_type": "cams", "device_id": cam, "email": "nope"}}})
    assert r.json()["code"] == "109" and "email" in r.json()["error"]["fields"]


async def test_invitation_delete_rights(client, setup, mk_user):
    owner, other, admin, cam = setup
    third = await mk_user("third@x.io")
    iid = (await client.post("/v1/invitations", headers=bearer(tok(owner)), json=inv_body(cam))).json()["data"]["invitation"]["id"]
    assert (await client.delete(f"/v1/invitations/{iid}", headers=bearer(tok(third)))).json()["code"] == NOT_FOUND
    assert (await client.delete(f"/v1/invitations/{iid}", headers=bearer(tok(other)))).json()["code"] == "007"
    assert (await client.get(f"/v1/invitations/{iid}", headers=bearer(tok(owner)))).json()["code"] == NOT_FOUND


async def test_invitation_requires_access_token(client, setup):
    _, _, _, cam = setup
    assert (await client.get("/v1/invitations")).json()["code"] == "046"
    assert (await client.post("/v1/invitations", json=inv_body(cam))).json()["code"] == "046"
