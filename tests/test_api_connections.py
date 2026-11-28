from base64 import urlsafe_b64encode

import pytest

from app.core.codes import (
    BULK_LIMIT_EXCEEDED, CONNECTION_INVALID, EMPTY_BODY, FORBIDDEN_RESOURCE, OBJECT_NOT_FOUND, VALIDATION_ERROR,
)
from tests.conftest import bearer, mint_test_token


async def tok(uid, role="user"):
    return await mint_test_token(uid, role)


META = {"access_id": "aid", "access_secret": "sec", "endpoint": "openapi.tuyaeu.com"}


def conn(name="c", **kw):
    return {"provider": "tuya_cloud", "name": name, "meta": dict(META), **kw}


def body(**obj):
    return {"data": {"connection": obj}}


def bbody(items, **opts):
    d = {"data": {"connections": items}}
    if opts:
        d["options"] = opts
    return d


@pytest.fixture
async def users(mk_user):
    return await mk_user("o@x.io"), await mk_user("t@x.io"), await mk_user("a@x.io", role="admin")


@pytest.fixture(autouse=True)
def _mock_ext(monkeypatch):
    calls = {"sync": [], "inval": []}

    async def sync(cid):
        calls["sync"].append(cid)

    async def inval(a, e, access_secret):
        calls["inval"].append((a, e))

    monkeypatch.setattr("app.routers.connections.connection_sync.sync_connection_now", sync)
    monkeypatch.setattr("app.routers.connections.tuya_client_pool.invalidate", inval)
    return calls


async def create(client, uid, **kw):
    r = await client.post("/v1/connections", headers=bearer(await tok(uid)), json=body(**conn(**kw)))
    assert r.status_code == 201, r.text
    return r.json()["data"]["connection"]["id"]


async def test_create_redacts_and_syncs(client, users, _mock_ext):
    o, _, _ = users
    r = await client.post("/v1/connections", headers=bearer(await tok(o)), json=body(**conn()))
    j = r.json()
    assert r.status_code == 201 and j["code"] == "039"
    c = j["data"]["connection"]
    assert "access_secret" not in c["meta"] and c["meta"]["access_id"] == "aid"
    assert _mock_ext["sync"] == [c["id"]]
    r = await client.post("/v1/connections", headers=bearer(await tok(o)), json=body(**conn(enabled=0)))
    assert r.json()["code"] == "039" and _mock_ext["sync"] == [c["id"]]


async def test_generated_connection_list_filter_and_contract(client, users):
    owner, other, admin = users
    mine = await create(client, owner, name="Mine", enabled=0)
    theirs = await create(client, other, name="Theirs", enabled=0)
    headers = bearer(await tok(owner))
    admin_headers = bearer(await tok(admin, "admin"))
    query = urlsafe_b64encode(f"id IN [{mine}, {theirs}]".encode()).decode().rstrip("=")
    for auth, ids in ((headers, [mine]), (admin_headers, [mine, theirs])):
        response = await client.get("/v1/connections", headers=auth, params={"filter": query})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert [row["id"] for row in data["connections"]] == ids
        assert data["settings"]["total"] == len(ids)
        assert all("access_secret" not in row["meta"] for row in data["connections"])

    options = await client.options("/v1/connections", headers=headers)
    assert {"GET", "POST"} <= set(options.headers["allow"].split(", "))
    schema = await client.get(options.headers["cursed-schema-url"], headers=headers)
    methods = schema.json()["data"]["schema"]["methods"]
    assert "meta" not in methods["GET"]["query"]["filter"]["fields"]
    assert "access_secret" not in str(methods["GET"]["responses"])
    create_body = methods["POST"]["request"]["body_schema"]
    assert create_body["required"] == ["data"]
    assert all(key in str(create_body) for key in ("access_secret", "password", "writeOnly"))


async def test_ufanet_credentials_create_patch_and_delete(client, users, sessionmaker_, _mock_ext):
    from app.models.connection import Connection, ConnectionCredentials

    owner, _, _ = users
    headers = bearer(await tok(owner))
    meta = {"contract": "00123", "password": "first-secret"}
    response = await client.post("/v1/connections", headers=headers, json=body(provider="ufanet-smart-home", name="home", meta=meta))
    assert response.status_code == 201, response.text
    assert "first-secret" not in response.text
    cid = response.json()["data"]["connection"]["id"]
    assert _mock_ext["sync"] == [cid]
    async with sessionmaker_() as db:
        row = await db.get(Connection, cid)
        credentials = await db.get(ConnectionCredentials, cid)
        assert row.meta == {"contract": "00123"}
        assert credentials.credentials == meta
    response = await client.patch(f"/v1/connections/{cid}", headers=headers, json=body(meta={"contract": "00123", "password": "second-secret"}))
    assert response.status_code == 200, response.text
    assert "second-secret" not in response.text
    async with sessionmaker_() as db:
        assert (await db.get(ConnectionCredentials, cid)).credentials["password"] == "second-secret"
    response = await client.post(f"/v1/connections/{cid}/sync", headers=headers)
    assert response.status_code == 200, response.text
    assert _mock_ext["sync"] == [cid, cid, cid]
    response = await client.delete(f"/v1/connections/{cid}", headers=headers)
    assert response.status_code == 200
    async with sessionmaker_() as db:
        assert await db.get(ConnectionCredentials, cid) is None


async def test_ufanet_invalid_input_hides_password(client, users):
    owner, _, _ = users
    headers = bearer(await tok(owner))
    response = await client.post("/v1/connections", headers=headers, json=body(provider="ufanet-smart-home", name="home", meta={"password": "hidden-secret"}))
    assert response.status_code == 422
    assert "hidden-secret" not in response.text


async def test_ufanet_edit_replaces_devices_and_revokes_access(client, users, sessionmaker_, monkeypatch):
    from sqlalchemy import select
    from app.models.cams import Cam
    from app.models.connection import ConnectionResource
    from app.models.invitation import Invitation
    from app.models.sharing import Sharing
    from app.models.switch import Switch
    from app.services.connection_sync.ufanet_cameras import materialize_ufanet_cameras
    from app.services.connection_sync.ufanet_skuds import materialize_ufanet_skuds

    owner, other, _ = users
    headers = bearer(await tok(owner))
    meta = {"contract": "old", "password": "old-password"}
    result = await client.post('/v1/connections', headers=headers, json=body(provider='ufanet-smart-home', name='home', meta=meta))
    cid = result.json()['data']['connection']['id']

    async def import_devices(connection_id):
        async with sessionmaker_() as db:
            assert not (await db.execute(select(ConnectionResource).where(ConnectionResource.connection_id == connection_id))).scalars().all()
            for kind in ('camera', 'skud'):
                db.add(ConnectionResource(connection_id=cid, external_id='same', resource_type=kind,
                    name=kind, capabilities={'open': True} if kind == 'skud' else {}, attributes={},
                    available=1, first_seen_at=1, last_seen_at=1))
            await db.flush()
            cam = (await materialize_ufanet_cameras(db, cid))[0]
            switch = (await materialize_ufanet_skuds(db, cid))[0]
            ids = (cam.id, switch.id)
            await db.commit()
            return ids

    camera_id, switch_id = await import_devices(cid)
    async with sessionmaker_() as db:
        (await db.get(Cam, camera_id)).enabled = 1
        (await db.get(Switch, switch_id)).enabled = 1
        await db.commit()
    for kind, device_id in [('cams', camera_id), ('switches', switch_id)]:
        inv = await client.post('/v1/invitations', headers=headers, json={'data': {'invitation': {
            'device_type': kind, 'device_id': device_id, 'email': 't@x.io'}}})
        assert inv.status_code == 201, inv.text
        share = await client.post('/v1/shares', headers=headers, json={'data': {'share': {
            'device_type': kind, 'device_id': device_id, 'type': 'stream' if kind == 'cams' else 'device_use'}}})
        assert share.status_code == 201, share.text

    imported = []
    async def sync(connection_id):
        imported.append(await import_devices(connection_id))
    monkeypatch.setattr('app.routers.connections.connection_sync.sync_connection_now', sync)

    # No-op patches preserve devices; an actual credential change removes all
    # old grants before synchronously importing the new account's catalog.
    no_op = await client.patch(f'/v1/connections/{cid}', headers=headers, json=body(meta=meta))
    assert no_op.status_code == 200 and not imported
    changed = await client.patch(f'/v1/connections/{cid}', headers=headers,
        json=body(meta={'contract': 'new', 'password': 'new-password'}))
    assert changed.status_code == 200, changed.text
    assert len(imported) == 1
    async with sessionmaker_() as db:
        assert not (await db.execute(select(Invitation))).scalars().all()
        assert not (await db.execute(select(Sharing))).scalars().all()
        new_cam, new_switch = imported[0]
        assert (await db.get(Cam, new_cam)).enabled == 1
        assert (await db.get(Switch, new_switch)).enabled == 1

    # Bulk changes follow the same lifecycle. Disabling does not import until
    # the connection is enabled again.
    changed = await client.patch(f'/v1/connections/bulk/{cid}', headers=headers,
        json=bbody([{'enabled': 0}]))
    assert changed.status_code == 200, changed.text
    assert len(imported) == 1
    async with sessionmaker_() as db:
        assert not (await db.execute(select(ConnectionResource).where(ConnectionResource.connection_id == cid))).scalars().all()
    changed = await client.patch(f'/v1/connections/{cid}', headers=headers, json=body(enabled=1))
    assert changed.status_code == 200, changed.text
    assert len(imported) == 2


async def test_create_validation(client, users):
    o, _, _ = users
    h = bearer(await tok(o))
    r = await client.post("/v1/connections", headers=h, json={"data": {}})
    assert r.status_code == 422 and "connection" in r.json()["error"]["fields"]
    r = await client.post("/v1/connections", headers=h, json=body(provider="tuya_cloud", name="x", meta={"access_id": "a"}))
    assert r.json()["code"] == VALIDATION_ERROR
    r = await client.post("/v1/connections", headers=h, content=b"")
    assert r.json()["code"] == EMPTY_BODY


async def test_list_get_visibility(client, users):
    o, t, a = users
    cid = await create(client, o, name="mine")
    h = bearer(await tok(o))
    r = await client.get("/v1/connections?search=min&search_column=name", headers=h)
    d = r.json()["data"]
    assert r.json()["code"] == "000" and d["settings"]["total"] == 1 and d["connections"][0]["id"] == cid
    r = await client.get("/v1/connections", headers=bearer(await tok(t)))
    assert r.json()["code"] == "019"
    r = await client.get(f"/v1/connections/{cid}", headers=bearer(await tok(t)))
    assert r.status_code == 404 and r.json()["code"] == OBJECT_NOT_FOUND
    r = await client.get(f"/v1/connections/{cid}", headers=bearer(await tok(a, "admin")))
    assert r.json()["data"]["connection"]["id"] == cid


async def test_patch(client, users, _mock_ext):
    o, _, _ = users
    cid = await create(client, o)
    h = bearer(await tok(o))
    _mock_ext["sync"].clear()
    r = await client.patch(f"/v1/connections/{cid}", headers=h, json=body(name="new"))
    assert r.json()["code"] == "002" and r.json()["data"]["connection"]["name"] == "new"
    assert _mock_ext["sync"] == []
    r = await client.patch(f"/v1/connections/{cid}", headers=h, json=body(name="new"))
    assert r.json()["code"] == "007"
    new_meta = {**META, "access_secret": "other"}
    r = await client.patch(f"/v1/connections/{cid}", headers=h, json=body(meta=new_meta))
    assert r.json()["code"] == "002" and _mock_ext["sync"] == [cid]
    assert ("aid", "openapi.tuyaeu.com") in _mock_ext["inval"]
    r = await client.patch(f"/v1/connections/{cid}", headers=h, json=body(meta={"access_id": "a"}))
    assert r.json()["code"] == CONNECTION_INVALID and r.status_code == 400
    r = await client.patch(f"/v1/connections/{cid}", headers=h, json=body(provider="x"))
    assert r.json()["code"] == VALIDATION_ERROR


async def test_delete(client, users, _mock_ext):
    o, t, _ = users
    cid = await create(client, o)
    r = await client.delete(f"/v1/connections/{cid}", headers=bearer(await tok(t)))
    assert r.json()["code"] == OBJECT_NOT_FOUND
    r = await client.delete(f"/v1/connections/{cid}", headers=bearer(await tok(o)))
    assert r.json()["code"] == "009" and r.json()["data"]["connection"] == {"id": cid}
    assert ("aid", "openapi.tuyaeu.com") in _mock_ext["inval"]
    r = await client.get(f"/v1/connections/{cid}", headers=bearer(await tok(o)))
    assert r.json()["code"] == OBJECT_NOT_FOUND


async def test_sync_endpoint(client, users, _mock_ext):
    o, _, _ = users
    cid = await create(client, o)
    dis = await create(client, o, enabled=0)
    _mock_ext["sync"].clear()
    r = await client.post(f"/v1/connections/{cid}/sync", headers=bearer(await tok(o)))
    assert r.json()["code"] == "000" and r.json()["data"]["connection"]["id"] == cid
    assert _mock_ext["sync"] == [cid]
    r = await client.post(f"/v1/connections/{dis}/sync", headers=bearer(await tok(o)))
    assert r.json()["code"] == CONNECTION_INVALID


async def test_bulk_create_get(client, users):
    o, _, _ = users
    h = bearer(await tok(o))
    r = await client.post("/v1/connections/bulk", headers=h, json=bbody([conn("a"), conn("b")]))
    j = r.json()
    assert r.status_code == 201 and j["code"] == "039"
    ids = ",".join(str(c["id"]) for c in j["data"]["connections"])
    r = await client.get(f"/v1/connections/bulk/{ids}", headers=h)
    assert [c["name"] for c in r.json()["data"]["connections"]] == ["a", "b"]
    bad = {"provider": "tuya_cloud", "name": "z", "meta": {}}
    r = await client.post("/v1/connections/bulk", headers=h, json=bbody([conn("ok"), bad]))
    assert r.status_code == 422 and r.json()["code"] == VALIDATION_ERROR
    r = await client.post("/v1/connections/bulk", headers=h, json=bbody([conn("ok"), bad], partial_allowed=True))
    j = r.json()
    assert j["code"] == "041" and j["data"]["connections"]["failed"][0]["id"] == 1
    r = await client.post("/v1/connections/bulk", headers=h, json=bbody([conn()] * 51))
    assert r.json()["code"] == BULK_LIMIT_EXCEEDED


async def test_bulk_patch_delete(client, users):
    o, t, _ = users
    a, b = await create(client, o, name="a"), await create(client, o, name="b")
    foreign = await create(client, t)
    h = bearer(await tok(o))
    r = await client.patch(f"/v1/connections/bulk/{a},{b}", headers=h, json=bbody([{"name": "A"}, {"name": "B"}]))
    assert r.json()["code"] == "002"
    r = await client.patch(f"/v1/connections/bulk/{a},{foreign}", headers=h, json=bbody([{"name": "Q"}, {"name": "Q"}]))
    assert r.json()["code"] == OBJECT_NOT_FOUND
    assert (await client.get(f"/v1/connections/{a}", headers=h)).json()["data"]["connection"]["name"] == "A"
    r = await client.patch(f"/v1/connections/bulk/{a},{foreign}?partial_allowed=true", headers=h,
                           json=bbody([{"name": "Q"}, {"name": "Q"}]))
    assert r.json()["code"] == "004"
    r = await client.delete(f"/v1/connections/bulk/{a},{foreign}", headers=h)
    assert r.json()["code"] == OBJECT_NOT_FOUND
    r = await client.delete(f"/v1/connections/bulk/{a},{foreign}?partial_allowed=true", headers=h)
    assert r.json()["code"] == "012"
    r = await client.delete(f"/v1/connections/bulk/{b}", headers=h)
    assert r.json()["code"] == "009" and r.json()["data"]["connections"] == [{"id": b}]


async def test_bulk_ids_validation(client, users):
    o, _, _ = users
    h = bearer(await tok(o))
    r = await client.get("/v1/connections/bulk/1,x", headers=h)
    assert r.json()["code"] == VALIDATION_ERROR and "ids" in r.json()["error"]["path"]
    r = await client.get("/v1/connections/bulk/" + ",".join(str(i) for i in range(1, 53)), headers=h)
    assert r.json()["code"] == BULK_LIMIT_EXCEEDED
    r = await client.get("/v1/connections/bulk/999", headers=h)
    assert r.json()["code"] == OBJECT_NOT_FOUND


async def test_bulk_patch_length_mismatch_and_admin(client, users):
    o, _, a = users
    cid = await create(client, o)
    r = await client.patch(f"/v1/connections/bulk/{cid}", headers=bearer(await tok(o)), json=bbody([{"name": "a"}, {"name": "b"}]))
    assert r.json()["code"] == VALIDATION_ERROR
    r = await client.patch(f"/v1/connections/{cid}", headers=bearer(await tok(a, "admin")), json=body(name="adm"))
    assert r.json()["code"] == "002"
