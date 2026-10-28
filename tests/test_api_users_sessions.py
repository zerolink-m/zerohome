import pytest

from tests.conftest import bearer, login_access


def ok_env(r, code):
    b = r.json()
    assert set(b) == {"code", "code_detail", "error", "metadata", "data"}
    assert b["code"] == code, b
    return b


def body(user, **opts):
    d = {"data": {"user": user}}
    if opts:
        d["options"] = opts
    return d


@pytest.fixture
async def admin(client, mk_user):
    uid = await mk_user("admin@x.io", role="admin")
    return uid, bearer(await login_access(client, "admin@x.io"))


@pytest.fixture
async def user(client, mk_user):
    uid = await mk_user("u@x.io")
    return uid, bearer(await login_access(client, "u@x.io"))


async def test_users_crud_admin(client, admin):
    _, h = admin
    r = await client.post("/v1/users", json=body({"name": "N", "surname": "S", "email": "New@X.io", "password": "pw", "role": "user"}), headers=h)
    assert r.status_code == 201
    u = ok_env(r, "001")["data"]["user"]
    assert u["email"] == "new@x.io" and "password" not in u
    r = await client.post("/v1/users", json=body({"name": "N", "surname": "S", "email": "new@x.io", "password": "pw"}), headers=h)
    ok_env(r, "139")
    r = await client.get(f"/v1/users/{u['id']}", headers=h)
    assert ok_env(r, "000")["data"]["user"]["id"] == u["id"]
    r = await client.patch(f"/v1/users/{u['id']}", json=body({"name": "Z"}), headers=h)
    d = ok_env(r, "003")["data"]["user"]
    assert d["name"] == "Z"
    r = await client.patch(f"/v1/users/{u['id']}", json=body({"name": "Z"}), headers=h)
    ok_env(r, "005")
    r = await client.delete(f"/v1/users/{u['id']}", headers=h)
    assert ok_env(r, "007")["data"] == {"user": {"id": u["id"]}}
    r = await client.get(f"/v1/users/{u['id']}", headers=h)
    assert r.status_code == 404
    ok_env(r, "082")


async def test_users_create_validation_and_perms(client, admin, user):
    r = await client.post("/v1/users", json=body({"name": "N"}), headers=admin[1])
    assert r.status_code == 422
    assert "email" in ok_env(r, "109")["error"]["fields"]
    r = await client.post("/v1/users", json={"name": "N"}, headers=admin[1])
    ok_env(r, "109")
    r = await client.post("/v1/users", json=body({"name": "N", "surname": "S", "email": "a@b.co", "password": "x"}), headers=user[1])
    assert r.status_code == 403
    ok_env(r, "050")


async def test_user_access_rules_and_patch_admin_fields(client, admin, user, mk_user):
    uid, h = user
    other = await mk_user("o@x.io")
    r = await client.get(f"/v1/users/{other}", headers=h)
    ok_env(r, "051")
    r = await client.patch(f"/v1/users/{uid}", json=body({"name": "Q", "role": "admin"}), headers=h)
    b = ok_env(r, "003")
    assert b["data"]["user"]["role"] == "user" and b["data"]["user"]["name"] == "Q"
    assert any("role" in w for w in b["metadata"]["warnings"])


async def test_users_list_settings_and_multisearch(client, admin, mk_user):
    for i in range(3):
        await mk_user(f"p{i}@x.io", name=f"n{i}")
    r = await client.get("/v1/users?limit=2&sort_by=id&order=desc", headers=admin[1])
    b = ok_env(r, "000")
    assert len(b["data"]["users"]) == 2
    s = b["data"]["settings"]
    assert s["total"] == 4 and s["limit"] == 2 and s["order"] == "descending" and s["sort_by"] == "id"
    r = await client.get("/v1/users?search=n0,n1&search_column=name,name&search_method=exactly,or,exactly", headers=admin[1])
    assert {u["name"] for u in ok_env(r, "000")["data"]["users"]} == {"n0", "n1"}
    r = await client.get("/v1/users?search=zzz&search_column=name", headers=admin[1])
    ok_env(r, "012")
    r = await client.get("/v1/users?search=x&search_column=password", headers=admin[1])
    ok_env(r, "144")
    r = await client.get("/v1/users?sort_by=password", headers=admin[1])
    ok_env(r, "143")
    r = await client.get("/v1/users?limit=100000", headers=admin[1])
    ok_env(r, "183")


async def test_users_list_non_admin_sees_self(client, user, mk_user):
    await mk_user("o@x.io")
    r = await client.get("/v1/users", headers=user[1])
    b = ok_env(r, "000")
    assert [u["email"] for u in b["data"]["users"]] == ["u@x.io"] and b["data"]["settings"]["total"] == 1


async def test_users_bulk_create_atomic_and_partial(client, admin):
    h = admin[1]
    good = {"name": "N", "surname": "S", "email": "b1@x.io", "password": "x"}
    bad = {"name": "N", "surname": "S", "email": "nope", "password": "x"}
    r = await client.post("/v1/users/bulk", json={"data": {"users": [good, bad]}}, headers=h)
    assert r.status_code == 422
    ok_env(r, "109")
    r = await client.get("/v1/users?search=b1@x.io&search_column=email", headers=h)
    ok_env(r, "012")  # atomic: nothing created
    r = await client.post("/v1/users/bulk", json={"data": {"users": [good, bad]}, "options": {"partial_allowed": True}}, headers=h)
    b = ok_env(r, "002")
    assert len(b["data"]["users"]["succeeded"]) == 1 and b["data"]["users"]["failed"][0]["id"] == 1
    r = await client.post("/v1/users/bulk", json={"data": {"users": [{**good, "email": "b2@x.io"}, {**good, "email": "b3@x.io"}]}}, headers=h)
    assert r.status_code == 201 and len(ok_env(r, "001")["data"]["users"]) == 2
    # duplicate email in partial mode -> failed element
    r = await client.post("/v1/users/bulk", json={"data": {"users": [{**good, "email": "b4@x.io"}, good]}, "options": {"partial_allowed": True}}, headers=h)
    b = ok_env(r, "002")
    assert b["data"]["users"]["failed"][0]["id"] == 1


async def test_users_bulk_get_patch_delete(client, admin, mk_user):
    h = admin[1]
    a, b_ = await mk_user("a@x.io"), await mk_user("b@x.io")
    r = await client.get(f"/v1/users/bulk/{a},{b_}", headers=h)
    b = ok_env(r, "000")
    assert [u["id"] for u in b["data"]["users"]] == [a, b_] and b["data"]["settings"]["total"] == 2
    r = await client.get(f"/v1/users/bulk/{a},9999", headers=h)
    ok_env(r, "082")
    r = await client.get("/v1/users/bulk/a,b", headers=h)
    assert r.status_code == 422
    assert "ids" in ok_env(r, "109")["error"]["path"]
    r = await client.patch(f"/v1/users/bulk/{a},{b_}", json=body({"name": "Bulk"}), headers=h)
    assert [u["name"] for u in ok_env(r, "003")["data"]["users"]] == ["Bulk", "Bulk"]
    r = await client.patch(f"/v1/users/bulk/{a},9999", json=body({"name": "X"}), headers=h)
    ok_env(r, "082")
    r = await client.get(f"/v1/users/{a}", headers=h)
    assert r.json()["data"]["user"]["name"] == "Bulk"  # atomic: unchanged by failed request
    r = await client.patch(f"/v1/users/bulk/{a},9999", json=body({"name": "X"}, partial_allowed=True), headers=h)
    b = ok_env(r, "004")
    assert b["data"]["users"]["failed"][0]["id"] == 9999
    r = await client.delete(f"/v1/users/bulk/{a},9999", headers=h)
    ok_env(r, "082")
    r = await client.delete(f"/v1/users/bulk/{a},9999?partial_allowed=true", headers=h)
    b = ok_env(r, "008")
    assert b["data"]["users"]["succeeded"] == [{"id": a}]
    r = await client.delete(f"/v1/users/bulk/{b_}", headers=h)
    assert ok_env(r, "007")["data"]["users"] == [{"id": b_}]


async def test_options_users(client):
    r = await client.options("/v1/users")
    assert "GET" in r.headers["allow"] and "POST" in r.headers["allow"]
    fns = r.headers["cursed-supported-functions"].split(",")
    assert {"limit", "offset", "sort_by", "search", "multi_search"} <= set(fns)
    r = await client.options("/v1/users/bulk")
    assert r.headers["cursed-bulk"] == "on" and r.headers["cursed-max-bulk"] == "100"
    r = await client.options("/v1/users/bulk/1,2")
    assert r.headers["cursed-bulk"] == "on"


async def test_sessions(client, admin, user, mk_user, db):
    uid, h = user
    r = await client.get(f"/v1/users/{uid}/sessions", headers=h)
    b = ok_env(r, "000")
    assert len(b["data"]["sessions"]) == 1 and b["data"]["settings"]["total"] == 1
    sid = b["data"]["sessions"][0]["id"]
    r = await client.get(f"/v1/users/{uid}/sessions?search=pytest&search_column=ua", headers=h)
    ok_env(r, "000")
    r = await client.get(f"/v1/users/{uid}/sessions/{sid}", headers=h)
    assert ok_env(r, "000")["data"]["session"]["id"] == sid
    r = await client.get(f"/v1/users/{uid}/sessions/999", headers=h)
    assert r.status_code == 404
    ok_env(r, "082")
    r = await client.get(f"/v1/users/{admin[0]}/sessions", headers=h)
    ok_env(r, "051")
    r = await client.get("/v1/users/9999/sessions", headers=admin[1])
    ok_env(r, "082")
    # bulk
    r = await client.get(f"/v1/users/{uid}/sessions/bulk/{sid}", headers=h)
    assert ok_env(r, "000")["data"]["settings"]["total"] == 1
    r = await client.delete(f"/v1/users/{uid}/sessions/bulk/{sid},999", headers=h)
    ok_env(r, "082")
    r = await client.delete(f"/v1/users/{uid}/sessions/bulk/{sid},999?partial_allowed=true", headers=h)
    b = ok_env(r, "008")
    assert b["data"]["sessions"]["succeeded"] == [{"id": sid}]
    # access token is still valid (stateless) -> delete single of unknown
    r = await client.delete(f"/v1/users/{uid}/sessions/{sid}", headers=h)
    ok_env(r, "082")


async def test_session_delete_single(client, user):
    uid, h = user
    sid = (await client.get(f"/v1/users/{uid}/sessions", headers=h)).json()["data"]["sessions"][0]["id"]
    r = await client.delete(f"/v1/users/{uid}/sessions/{sid}", headers=h)
    assert ok_env(r, "007")["data"] == {"session": {"id": sid}}
