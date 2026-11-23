from tests.conftest import mint_test_token
import time

import pytest

from app.core.codes import (
    BULK_LIMIT_EXCEEDED, EMPTY_BODY, FORBIDDEN_RESOURCE, OBJECT_NOT_FOUND, TUYA_LOCAL_NOT_ALLOWED, VALIDATION_ERROR,
)
from app.models.switch import Switch
from tests.conftest import bearer


async def tok(uid, role="user"):
    return await mint_test_token(uid, role)


def sw(name="s", **kw):
    return {"control_protocol": "custom", "name": name, "control_metadata": {
        "on": {"request": {"url": "http://x/on", "method": "GET"}},
        "off": {"request": {"url": "http://x/off", "method": "GET"}}}, **kw}


def body(**obj):
    return {"data": {"switch": obj}}


def bbody(items, **opts):
    d = {"data": {"switches": items}}
    if opts:
        d["options"] = opts
    return d


@pytest.fixture
async def users(mk_user):
    return await mk_user("o@x.io"), await mk_user("t@x.io"), await mk_user("a@x.io", role="admin")


@pytest.fixture(autouse=True)
def _mock_ext(monkeypatch):
    calls = []

    async def set_state(switch, state):
        calls.append((switch.id, state))

    monkeypatch.setattr("app.routers.switches.switch_client.set_state", set_state)
    return calls


async def create(client, uid, **kw):
    r = await client.post("/v1/switches", headers=bearer(await tok(uid)), json=body(**sw(**kw)))
    assert r.status_code == 201, r.text
    return r.json()["data"]["switch"]["id"]


async def test_create_get_list(client, users):
    o, t, _ = users
    r = await client.post("/v1/switches", headers=bearer(await tok(o)), json=body(**sw("a")))
    j = r.json()
    assert r.status_code == 201 and j["code"] == "039"
    sid = j["data"]["switch"]["id"]
    assert j["data"]["switch"]["owner"] == o and j["data"]["switch"]["control_protocol"] == "custom"

    r = await client.get(f"/v1/switches/{sid}", headers=bearer(await tok(o)))
    assert r.json()["code"] == "000" and r.json()["data"]["switch"]["name"] == "a"

    r = await client.get("/v1/switches", headers=bearer(await tok(o)))
    d = r.json()["data"]
    assert r.json()["code"] == "000" and d["settings"]["total"] == 1 and len(d["switches"]) == 1

    r = await client.get("/v1/switches", headers=bearer(await tok(t)))
    assert r.json()["code"] == "019" and r.json()["data"]["switches"] == []


async def test_list_search_sort_limit(client, users):
    o, _, _ = users
    for n in ("alpha", "beta", "gamma"):
        await create(client, o, name=n)
    h = bearer(await tok(o))
    r = await client.get("/v1/switches?search=a&search_column=name&search_method=starts", headers=h)
    assert [s["name"] for s in r.json()["data"]["switches"]] == ["alpha"]
    r = await client.get("/v1/switches?sort_by=name&order=desc&limit=2", headers=h)
    d = r.json()["data"]
    assert [s["name"] for s in d["switches"]] == ["gamma", "beta"] and d["settings"]["total"] == 3
    r = await client.get("/v1/switches?limit=1000", headers=h)
    assert r.status_code == 400 and r.json()["code"] == "158"
    r = await client.get("/v1/switches?sort_by=bogus", headers=h)
    assert r.json()["code"] == "125"


async def test_create_validation_errors(client, users):
    o, _, _ = users
    h = bearer(await tok(o))
    r = await client.post("/v1/switches", headers=h, json={"data": {}})
    assert r.status_code == 422 and r.json()["code"] == VALIDATION_ERROR
    assert "switch" in r.json()["error"]["fields"]
    r = await client.post("/v1/switches", headers=h, json=body(control_protocol="nope"))
    assert r.json()["code"] == VALIDATION_ERROR
    assert "control_protocol" in r.json()["error"]["fields"]
    r = await client.post("/v1/switches", headers=h, content=b"")
    assert r.json()["code"] == EMPTY_BODY


async def test_tuya_local_forbidden(client, users):
    o, _, _ = users
    meta = {"device_id": "d", "ip": "1.1.1.1", "local_key": "k", "version": "3.3"}
    r = await client.post("/v1/switches", headers=bearer(await tok(o)),
                          json=body(control_protocol="tuya_local", control_metadata=meta))
    assert r.json()["code"] == TUYA_LOCAL_NOT_ALLOWED and r.status_code == 403


async def test_not_found_and_foreign(client, users):
    o, t, a = users
    sid = await create(client, o)
    r = await client.get(f"/v1/switches/{sid}", headers=bearer(await tok(t)))
    assert r.status_code == 404 and r.json()["code"] == OBJECT_NOT_FOUND
    r = await client.get(f"/v1/switches/{sid}", headers=bearer(await tok(a, "admin")))
    assert r.json()["code"] == "000"
    r = await client.get("/v1/switches/999", headers=bearer(await tok(o)))
    assert r.json()["code"] == OBJECT_NOT_FOUND


async def test_patch_and_not_changed(client, users):
    o, _, _ = users
    sid = await create(client, o, name="a")
    h = bearer(await tok(o))
    r = await client.patch(f"/v1/switches/{sid}", headers=h, json=body(name="b", enabled=1))
    assert r.json()["code"] == "002" and r.json()["data"]["switch"]["name"] == "b"
    r = await client.patch(f"/v1/switches/{sid}", headers=h, json=body(name="b"))
    assert r.json()["code"] == "007"
    r = await client.patch(f"/v1/switches/{sid}", headers=h, json=body(bogus=1))
    assert r.json()["code"] == VALIDATION_ERROR


async def test_delete(client, users, sessionmaker_):
    o, t, _ = users
    sid = await create(client, o)
    r = await client.delete(f"/v1/switches/{sid}", headers=bearer(await tok(t)))
    assert r.json()["code"] == OBJECT_NOT_FOUND
    r = await client.delete(f"/v1/switches/{sid}", headers=bearer(await tok(o)))
    assert r.json()["code"] == "009" and r.json()["data"]["switch"] == {"id": sid}
    r = await client.get(f"/v1/switches/{sid}", headers=bearer(await tok(o)))
    assert r.json()["code"] == OBJECT_NOT_FOUND


async def make_auto(client, sessionmaker_, uid):
    """Switch with added_by='auto' needs a real connection + external_id (CHECK ck_switches_added_by_consistency)."""
    sid = await create(client, uid)
    r = await client.post("/v1/connections", headers=bearer(await tok(uid)), json={"data": {"connection": {
        "provider": "tuya_cloud", "name": "c", "enabled": 0,
        "meta": {"access_id": "aid", "access_secret": "sec", "endpoint": "openapi.tuyaeu.com"}}}})
    assert r.status_code == 201, r.text
    cid = r.json()["data"]["connection"]["id"]
    async with sessionmaker_() as s:
        row = await s.get(Switch, sid)
        row.added_by = "auto"
        row.connection_id = cid
        row.external_id = "dev1"
        row.is_master = 1
        await s.commit()
    return sid


async def test_delete_auto_forbidden(client, users, sessionmaker_):
    o, _, _ = users
    sid = await make_auto(client, sessionmaker_, o)
    r = await client.delete(f"/v1/switches/{sid}", headers=bearer(await tok(o)))
    assert r.status_code == 403 and r.json()["code"] == FORBIDDEN_RESOURCE


async def test_patch_auto_restricted_fields(client, users, sessionmaker_):
    o, _, _ = users
    sid = await make_auto(client, sessionmaker_, o)
    h = bearer(await tok(o))
    r = await client.patch(f"/v1/switches/{sid}", headers=h, json=body(control_protocol="custom"))
    assert r.status_code == 403 and r.json()["code"] == FORBIDDEN_RESOURCE
    r = await client.patch(f"/v1/switches/{sid}", headers=h, json=body(name="renamed"))
    assert r.json()["code"] == "002"


async def test_bulk_delete_auto_partial(client, users, sessionmaker_):
    o, _, _ = users
    auto, plain = await make_auto(client, sessionmaker_, o), await create(client, o)
    h = bearer(await tok(o))
    r = await client.delete(f"/v1/switches/bulk/{auto},{plain}", headers=h)
    assert r.json()["code"] == FORBIDDEN_RESOURCE
    r = await client.delete(f"/v1/switches/bulk/{auto},{plain}?partial_allowed=true", headers=h)
    j = r.json()
    assert j["code"] == "012" and j["data"]["switches"]["succeeded"] == [{"id": plain}]
    assert j["data"]["switches"]["failed"][0]["id"] == auto


async def test_bulk_ids_limits(client, users):
    o, _, _ = users
    h = bearer(await tok(o))
    r = await client.get("/v1/switches/bulk/" + ",".join(str(i) for i in range(1, 102)), headers=h)
    assert r.json()["code"] == BULK_LIMIT_EXCEEDED
    # duplicates are dropped (not an error): "1,1" == "1" -> not found for a missing id
    r = await client.get("/v1/switches/bulk/1,1", headers=h)
    assert r.json()["code"] == OBJECT_NOT_FOUND
    r = await client.get("/v1/switches/bulk/1,,2", headers=h)
    assert r.json()["code"] == VALIDATION_ERROR and "ids" in r.json()["error"]["path"]


async def test_bulk_create_tuya_local_partial(client, users):
    o, _, _ = users
    h = bearer(await tok(o))
    meta = {"device_id": "d", "ip": "1.1.1.1", "local_key": "k", "version": "3.3"}
    items = [sw("ok"), {"control_protocol": "tuya_local", "control_metadata": meta}]
    r = await client.post("/v1/switches/bulk", headers=h, json=bbody(items))
    assert r.json()["code"] == TUYA_LOCAL_NOT_ALLOWED
    r = await client.post("/v1/switches/bulk", headers=h, json=bbody(items, partial_allowed=True))
    j = r.json()
    assert j["code"] == "041" and len(j["data"]["switches"]["succeeded"]) == 1
    assert j["data"]["switches"]["failed"][0]["id"] == 1


async def test_state(client, users, _mock_ext):
    o, t, _ = users
    sid = await create(client, o)
    h = bearer(await tok(o))
    r = await client.get(f"/v1/switches/{sid}/state", headers=h)
    assert r.json()["code"] == "000" and r.json()["data"]["state"]["state"] == 0
    r = await client.post(f"/v1/switches/{sid}/state", headers=h, json={"data": {"state": {"state": 1}}})
    assert r.json()["code"] == "000" and r.json()["data"]["state"]["state"] == 1
    assert _mock_ext == [(sid, 1)]
    r = await client.post(f"/v1/switches/{sid}/state", headers=h, json={"data": {"state": {"state": 5}}})
    assert r.json()["code"] == VALIDATION_ERROR
    r = await client.post(f"/v1/switches/{sid}/state", headers=bearer(await tok(t)), json={"data": {"state": {"state": 1}}})
    assert r.status_code in (403, 404)


# ---------------- bulk ----------------
async def test_bulk_create_and_get(client, users):
    o, _, _ = users
    h = bearer(await tok(o))
    r = await client.post("/v1/switches/bulk", headers=h, json=bbody([sw("a"), sw("b")]))
    j = r.json()
    assert r.status_code == 201 and j["code"] == "039" and len(j["data"]["switches"]) == 2
    ids = ",".join(str(s["id"]) for s in j["data"]["switches"])
    r = await client.get(f"/v1/switches/bulk/{ids}", headers=h)
    assert r.json()["code"] == "000" and [s["name"] for s in r.json()["data"]["switches"]] == ["a", "b"]
    r = await client.get(f"/v1/switches/bulk/{ids},999", headers=h)
    assert r.json()["code"] == OBJECT_NOT_FOUND
    r = await client.get("/v1/switches/bulk/1,x", headers=h)
    assert r.json()["code"] == VALIDATION_ERROR and "ids" in r.json()["error"]["path"]


async def test_bulk_create_atomic_and_partial(client, users):
    o, _, _ = users
    h = bearer(await tok(o))
    items = [sw("ok"), {"control_protocol": "bad"}]
    r = await client.post("/v1/switches/bulk", headers=h, json=bbody(items))
    assert r.status_code == 422 and r.json()["code"] == VALIDATION_ERROR
    assert (await client.get("/v1/switches", headers=h)).json()["data"]["settings"]["total"] == 0
    r = await client.post("/v1/switches/bulk", headers=h, json=bbody(items, partial_allowed=True))
    j = r.json()
    assert j["code"] == "041"
    assert len(j["data"]["switches"]["succeeded"]) == 1 and j["data"]["switches"]["failed"][0]["id"] == 1
    r = await client.post("/v1/switches/bulk", headers=h, json=bbody([{"control_protocol": "bad"}], partial_allowed=True))
    assert r.json()["code"] == VALIDATION_ERROR
    r = await client.post("/v1/switches/bulk", headers=h, json=bbody([]))
    assert r.json()["code"] == EMPTY_BODY
    r = await client.post("/v1/switches/bulk", headers=h, json=bbody([sw()] * 101))
    assert r.json()["code"] == BULK_LIMIT_EXCEEDED


async def test_bulk_patch(client, users):
    o, t, _ = users
    a, b = await create(client, o, name="a"), await create(client, o, name="b")
    other = await create(client, t, name="x")
    h = bearer(await tok(o))
    r = await client.patch(f"/v1/switches/bulk/{a},{b}", headers=h, json=bbody([{"name": "A"}, {"name": "B"}]))
    assert r.json()["code"] == "002" and [s["name"] for s in r.json()["data"]["switches"]] == ["A", "B"]
    r = await client.patch(f"/v1/switches/bulk/{a},{b}", headers=h, json=bbody([{"name": "A"}]))
    assert r.json()["code"] == VALIDATION_ERROR
    # atomic: foreign id rejects everything
    r = await client.patch(f"/v1/switches/bulk/{a},{other}", headers=h, json=bbody([{"name": "Z"}, {"name": "Z"}]))
    assert r.json()["code"] == OBJECT_NOT_FOUND
    assert (await client.get(f"/v1/switches/{a}", headers=h)).json()["data"]["switch"]["name"] == "A"
    r = await client.patch(f"/v1/switches/bulk/{a},{other}?partial_allowed=true", headers=h,
                           json=bbody([{"name": "Z"}, {"name": "Z"}]))
    j = r.json()
    assert j["code"] == "004" and j["data"]["switches"]["failed"][0]["id"] == other


async def test_bulk_delete(client, users):
    o, _, _ = users
    a, b = await create(client, o), await create(client, o)
    h = bearer(await tok(o))
    r = await client.delete(f"/v1/switches/bulk/{a},999", headers=h)
    assert r.json()["code"] == OBJECT_NOT_FOUND
    assert (await client.get(f"/v1/switches/{a}", headers=h)).json()["code"] == "000"
    r = await client.delete(f"/v1/switches/bulk/{a},999?partial_allowed=true", headers=h)
    assert r.json()["code"] == "012" and r.json()["data"]["switches"]["succeeded"] == [{"id": a}]
    r = await client.delete(f"/v1/switches/bulk/{b}", headers=h)
    assert r.json()["code"] == "009" and r.json()["data"]["switches"] == [{"id": b}]
