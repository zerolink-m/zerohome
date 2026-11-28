from tests.conftest import mint_test_token
import time

import pytest

from app.core.codes import (
    FORBIDDEN_RESOURCE, INVITATION_ALREADY_EXISTS, INVITATION_INVALID,
    INVITATION_OWNER_FORBIDDEN, OBJECT_NOT_FOUND as NOT_FOUND, SHARE_INVALID,
)
from app.core.security import decode_share_token
from tests.conftest import bearer


async def tok(uid, role="user"):
    return await mint_test_token(uid, role)


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
    r = await client.post("/v1/shares", headers=bearer(await tok(owner)), json=share_body(cam))
    assert r.status_code == 201 and r.json()["code"] == "039"
    d = r.json()["data"]["share"]
    p = decode_share_token(d["token"])
    assert (p.sha_id, p.user_id, p.type, p.device_type, p.device_id) == (d["id"], owner, "stream", "cams", cam)
    assert p.exp - p.iat == 60  # SHARE_TOKEN_MIN_TTL_SECONDS in tests
    assert d["expires_at"] == p.exp or abs(d["expires_at"] - p.exp) <= 1


@pytest.mark.parametrize("ttl", [59, 3601])
async def test_share_ttl_out_of_range(client, setup, ttl):
    owner, _, _, cam = setup
    r = await client.post("/v1/shares", headers=bearer(await tok(owner)), json=share_body(cam, ttl_seconds=ttl))
    assert r.json()["code"] == SHARE_INVALID


@pytest.mark.parametrize("ttl", [60, 3600])
async def test_share_ttl_boundaries_ok(client, setup, ttl):
    owner, _, _, cam = setup
    r = await client.post("/v1/shares", headers=bearer(await tok(owner)), json=share_body(cam, ttl_seconds=ttl))
    assert r.json()["code"] == "039"


async def test_share_invalid_type_for_device(client, setup):
    owner, _, _, cam = setup
    r = await client.post("/v1/shares", headers=bearer(await tok(owner)),
                          json=_raw("cams", cam, "device_use"))
    assert r.json()["code"] == SHARE_INVALID
    r = await client.post("/v1/shares", headers=bearer(await tok(owner)),
                          json=_raw("switches", 1, "stream"))
    assert r.json()["code"] == SHARE_INVALID


async def test_share_validation_filename_rules(client, setup):
    owner, _, _, cam = setup
    r = await client.post("/v1/shares", headers=bearer(await tok(owner)), json=share_body(cam, filename="x.mp4"))
    assert r.json()["code"] == "269"
    r = await client.post("/v1/shares", headers=bearer(await tok(owner)), json=share_body(cam, type="record"))
    assert r.json()["code"] == "269"


async def test_share_only_owner_or_admin(client, setup):
    owner, other, admin, cam = setup
    r = await client.post("/v1/shares", headers=bearer(await tok(other)), json=share_body(cam))
    assert r.json()["code"] == FORBIDDEN_RESOURCE
    r = await client.post("/v1/shares", headers=bearer(await tok(admin, "admin")), json=share_body(cam))
    assert r.json()["code"] == "039"


async def test_share_unknown_device(client, setup):
    owner, *_ = setup
    r = await client.post("/v1/shares", headers=bearer(await tok(owner)), json=share_body(9999))
    assert r.json()["code"] == NOT_FOUND


async def test_share_requires_auth(client, setup):
    _, _, _, cam = setup
    r = await client.post("/v1/shares", json=share_body(cam))
    assert r.json()["code"] == "177"


async def test_share_list_get_delete_scoping(client, setup):
    owner, other, admin, cam = setup
    sid = (await client.post("/v1/shares", headers=bearer(await tok(owner)), json=share_body(cam))).json()["data"]["share"]["id"]
    assert (await client.get("/v1/shares", headers=bearer(await tok(owner)))).json()["data"]["settings"]["total"] == 1
    assert (await client.get("/v1/shares", headers=bearer(await tok(other)))).json()["data"]["settings"]["total"] == 0
    assert (await client.get("/v1/shares", headers=bearer(await tok(admin, "admin")))).json()["data"]["settings"]["total"] == 1
    assert (await client.get(f"/v1/shares/{sid}", headers=bearer(await tok(other)))).json()["code"] == NOT_FOUND
    assert (await client.delete(f"/v1/shares/{sid}", headers=bearer(await tok(other)))).json()["code"] == NOT_FOUND
    assert (await client.delete(f"/v1/shares/{sid}", headers=bearer(await tok(owner)))).json()["code"] == "009"
    assert (await client.get(f"/v1/shares/{sid}", headers=bearer(await tok(owner)))).json()["code"] == NOT_FOUND


async def test_share_token_itself_cannot_call_shares_api(client, setup):
    owner, _, _, cam = setup
    t = (await client.post("/v1/shares", headers=bearer(await tok(owner)), json=share_body(cam))).json()["data"]["share"]["token"]
    assert (await client.get("/v1/shares", headers=bearer(t))).json()["code"] == "167"


# ---------------- invitations ----------------
def inv_body(cam, email="other@x.io"):
    return {"data": {"invitation": {"device_type": "cams", "device_id": cam, "email": email}}}


async def test_invitation_create_rules(client, setup):
    owner, other, admin, cam = setup
    r = await client.post("/v1/invitations", headers=bearer(await tok(owner)), json=inv_body(cam))
    assert r.status_code == 201 and r.json()["code"] == "039"
    # duplicate (case-insensitive)
    r = await client.post("/v1/invitations", headers=bearer(await tok(owner)), json=inv_body(cam, "OTHER@x.io"))
    assert r.status_code == 400 and r.json()["code"] == INVITATION_ALREADY_EXISTS == "396"
    # inviting the owner
    r = await client.post("/v1/invitations", headers=bearer(await tok(owner)), json=inv_body(cam, "owner@x.io"))
    assert r.status_code == 400 and r.json()["code"] == INVITATION_OWNER_FORBIDDEN == "397"
    # non-owner
    r = await client.post("/v1/invitations", headers=bearer(await tok(other)), json=inv_body(cam, "z@x.io"))
    assert r.json()["code"] == FORBIDDEN_RESOURCE
    # unknown device
    r = await client.post("/v1/invitations", headers=bearer(await tok(owner)), json=inv_body(9999))
    assert r.json()["code"] == NOT_FOUND


async def test_invitation_bulk_preserves_specific_and_legacy_causes(client, setup):
    owner, _, _, cam = setup
    headers = bearer(await tok(owner))
    item = lambda email: {"device_type": "cams", "device_id": cam, "email": email}

    async def send(items, partial=False):
        return await client.post("/v1/invitations/bulk", headers=headers, json={
            "data": {"invitations": items}, "options": {"partial_allowed": partial},
        })

    # Repeating a pending item in an atomic request is not yet an existing invitation.
    response = await send([item("bulk@x.io"), item("bulk@x.io")])
    assert response.status_code == 400 and response.json()["code"] == INVITATION_INVALID == "390"
    response = await send([item("bulk@x.io"), item("bulk@x.io")], partial=True)
    assert response.status_code == 201 and response.json()["code"] == "041"
    assert response.json()["data"]["invitations"]["failed"][0]["id"] == 1
    for partial in (False, True):
        response = await send([item("BULK@x.io")], partial=partial)
        assert response.status_code == 400 and response.json()["code"] == INVITATION_ALREADY_EXISTS
    response = await send([item("bulk@x.io"), item("owner@x.io")], partial=True)
    assert response.status_code == 400 and response.json()["code"] == INVITATION_INVALID == "390"
    result = response.json()["data"]["invitations"]
    assert result["succeeded"] == [] and [entry["id"] for entry in result["failed"]] == [0, 1]


@pytest.mark.parametrize("email,expected", [
    ("other@x.io", INVITATION_ALREADY_EXISTS),
    ("owner@x.io", INVITATION_OWNER_FORBIDDEN),
])
async def test_invitation_bulk_all_failed_same_cause_keeps_specific_code(client, setup, email, expected):
    owner, _, _, cam = setup
    headers = bearer(await tok(owner))
    assert (await client.post("/v1/invitations", headers=headers, json=inv_body(cam))).status_code == 201
    item = {"device_type": "cams", "device_id": cam, "email": email}
    response = await client.post("/v1/invitations/bulk", headers=headers, json={
        "data": {"invitations": [item, item]}, "options": {"partial_allowed": True},
    })
    assert response.status_code == 400 and response.json()["code"] == expected
    result = response.json()["data"]["invitations"]
    assert result["succeeded"] == [] and [entry["id"] for entry in result["failed"]] == [0, 1]


@pytest.mark.parametrize("emails", [
    ("legacy@x.io", "other@x.io"),
    ("other@x.io", "legacy@x.io"),
    ("legacy@x.io", "owner@x.io"),
    ("owner@x.io", "legacy@x.io"),
    ("legacy@x.io", "other@x.io", "owner@x.io"),
    ("owner@x.io", "other@x.io"),
])
async def test_invitation_bulk_mixed_invitation_causes_use_legacy_code(client, setup, monkeypatch, emails):
    from app.core.errors import AppError
    from app.routers import invitations

    owner, _, _, cam = setup
    headers = bearer(await tok(owner))
    assert (await client.post("/v1/invitations", headers=headers, json=inv_body(cam))).status_code == 201
    original = invitations._build_invitation

    async def build(body, payload, db):
        if body.email == "legacy@x.io":
            raise AppError(INVITATION_INVALID, "Legacy invitation failure")
        return await original(body, payload, db)

    monkeypatch.setattr(invitations, "_build_invitation", build)
    items = [{"device_type": "cams", "device_id": cam, "email": email} for email in emails]
    response = await client.post("/v1/invitations/bulk", headers=headers, json={
        "data": {"invitations": items}, "options": {"partial_allowed": True},
    })
    assert response.status_code == 400 and response.json()["code"] == INVITATION_INVALID
    result = response.json()["data"]["invitations"]
    assert result["succeeded"] == []
    assert [entry["id"] for entry in result["failed"]] == list(range(len(emails)))


@pytest.mark.parametrize("other_cause", ["validation", "not_found", "forbidden"])
async def test_invitation_bulk_mixed_other_causes_keep_fallback(client, setup, mk_cam, other_cause):
    owner, other, _, cam = setup
    headers = bearer(await tok(owner))
    assert (await client.post("/v1/invitations", headers=headers, json=inv_body(cam))).status_code == 201
    existing = {"device_type": "cams", "device_id": cam, "email": "other@x.io"}
    if other_cause == "validation":
        other_item = {**existing, "email": "not-an-email"}
    elif other_cause == "not_found":
        other_item = {**existing, "device_id": 999999}
    else:
        other_item = {**existing, "device_id": await mk_cam(other)}
    response = await client.post("/v1/invitations/bulk", headers=headers, json={
        "data": {"invitations": [existing, other_item]}, "options": {"partial_allowed": True},
    })
    assert response.status_code == 422 and response.json()["code"] == "269"
    result = response.json()["data"]["invitations"]
    assert result["succeeded"] == [] and [entry["id"] for entry in result["failed"]] == [0, 1]


async def test_invitation_bulk_unrelated_homogeneous_errors_are_unchanged(client, setup):
    owner, _, _, _ = setup
    headers = bearer(await tok(owner))
    items = [{"device_type": "cams", "device_id": cam_id, "email": "other@x.io"}
             for cam_id in (999998, 999999)]
    response = await client.post("/v1/invitations/bulk", headers=headers, json={
        "data": {"invitations": items}, "options": {"partial_allowed": True},
    })
    assert response.status_code == 404 and response.json()["code"] == NOT_FOUND


async def test_invitation_bulk_partial_success_with_mixed_invitation_failures(client, setup):
    owner, _, _, cam = setup
    headers = bearer(await tok(owner))
    assert (await client.post("/v1/invitations", headers=headers, json=inv_body(cam))).status_code == 201
    items = [{"device_type": "cams", "device_id": cam, "email": email}
             for email in ("new@x.io", "other@x.io", "owner@x.io")]
    response = await client.post("/v1/invitations/bulk", headers=headers, json={
        "data": {"invitations": items}, "options": {"partial_allowed": True},
    })
    assert response.status_code == 201 and response.json()["code"] == "041"
    result = response.json()["data"]["invitations"]
    assert len(result["succeeded"]) == 1 and result["succeeded"][0]["email"] == "new@x.io"
    assert [entry["id"] for entry in result["failed"]] == [1, 2]
    assert all("code" not in entry and "error" in entry["error"] for entry in result["failed"])


@pytest.mark.parametrize("mode", ["single", "bulk_atomic", "bulk_partial"])
async def test_invitation_duplicate_race_uses_specific_code(client, setup, monkeypatch, mode):
    from app.routers import invitations

    owner, _, _, cam = setup
    headers = bearer(await tok(owner))
    first = await client.post("/v1/invitations", headers=headers, json=inv_body(cam))
    assert first.status_code == 201
    original = invitations._invitation_exists
    checks = 0

    async def stale_first_check(body, db):
        nonlocal checks
        checks += 1
        return False if checks == 1 else await original(body, db)

    monkeypatch.setattr(invitations, "_invitation_exists", stale_first_check)
    body = inv_body(cam)
    if mode == "single":
        path = "/v1/invitations"
    else:
        path = "/v1/invitations/bulk"
        body = {"data": {"invitations": [body["data"]["invitation"]]},
                "options": {"partial_allowed": mode == "bulk_partial"}}
    response = await client.post(path, headers=headers, json=body)
    assert response.status_code == 400 and response.json()["code"] == INVITATION_ALREADY_EXISTS
    assert checks == 2


async def test_invitation_visibility_and_patch_rights(client, setup, mk_user):
    owner, other, admin, cam = setup
    third = await mk_user("third@x.io")
    iid = (await client.post("/v1/invitations", headers=bearer(await tok(owner)), json=inv_body(cam))).json()["data"]["invitation"]["id"]
    # third party sees nothing
    assert (await client.get(f"/v1/invitations/{iid}", headers=bearer(await tok(third)))).json()["code"] == NOT_FOUND
    assert (await client.get("/v1/invitations", headers=bearer(await tok(third)))).json()["data"]["settings"]["total"] == 0
    # sender and recipient see it
    assert (await client.get(f"/v1/invitations/{iid}", headers=bearer(await tok(owner)))).json()["code"] == "000"
    assert (await client.get(f"/v1/invitations/{iid}", headers=bearer(await tok(other)))).json()["code"] == "000"
    # sender cannot accept on behalf of the invitee
    r = await client.post(f"/v1/invitations/{iid}/accept", headers=bearer(await tok(owner)))
    assert r.json()["code"] == FORBIDDEN_RESOURCE
    r = await client.post(f"/v1/invitations/{iid}/accept", headers=bearer(await tok(other)))
    assert r.json()["code"] == "002"
    assert r.json()["data"]["invitation"]["status"] == "accepted"
    # repeated accept: not changed
    r = await client.post(f"/v1/invitations/{iid}/accept", headers=bearer(await tok(other)))
    assert r.json()["code"] == "007"
    got = (await client.get(f"/v1/invitations/{iid}", headers=bearer(await tok(other)))).json()["data"]["invitation"]
    assert got["status"] == "accepted" and got["updated_at"] is not None
    # PATCH on an invitation no longer exists
    r = await client.patch(f"/v1/invitations/{iid}", headers=bearer(await tok(other)), json={"data": {"invitation": {}}})
    assert r.status_code == 405


async def test_invitation_decline(client, setup):
    owner, other, admin, cam = setup
    iid = (await client.post("/v1/invitations", headers=bearer(await tok(owner)), json=inv_body(cam))).json()["data"]["invitation"]["id"]
    assert (await client.post(f"/v1/invitations/{iid}/decline", headers=bearer(await tok(owner)))).json()["code"] == FORBIDDEN_RESOURCE
    r = await client.post(f"/v1/invitations/{iid}/decline", headers=bearer(await tok(other)))
    assert r.json()["code"] == "009"
    assert (await client.get(f"/v1/invitations/{iid}", headers=bearer(await tok(owner)))).json()["code"] == NOT_FOUND


async def test_invitation_body_validation(client, setup):
    owner, _, _, cam = setup
    r = await client.post("/v1/invitations", headers=bearer(await tok(owner)), json={"device_type": "cams"})
    assert r.json()["code"] == "269"
    r = await client.post("/v1/invitations", headers=bearer(await tok(owner)),
                          json={"data": {"invitation": {"device_type": "cams", "device_id": cam, "email": "nope"}}})
    assert r.json()["code"] == "269" and "email" in r.json()["error"]["fields"]


async def test_invitation_delete_rights(client, setup, mk_user):
    owner, other, admin, cam = setup
    third = await mk_user("third@x.io")
    iid = (await client.post("/v1/invitations", headers=bearer(await tok(owner)), json=inv_body(cam))).json()["data"]["invitation"]["id"]
    assert (await client.delete(f"/v1/invitations/{iid}", headers=bearer(await tok(third)))).json()["code"] == NOT_FOUND
    assert (await client.delete(f"/v1/invitations/{iid}", headers=bearer(await tok(other)))).json()["code"] == "009"
    assert (await client.get(f"/v1/invitations/{iid}", headers=bearer(await tok(owner)))).json()["code"] == NOT_FOUND


async def test_invitation_requires_access_token(client, setup):
    _, _, _, cam = setup
    assert (await client.get("/v1/invitations")).json()["code"] == "177"
    assert (await client.post("/v1/invitations", json=inv_body(cam))).json()["code"] == "177"
