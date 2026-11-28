from base64 import urlsafe_b64encode
import time

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
    u = ok_env(r, "039")["data"]["user"]
    assert u["email"] == "new@x.io" and "password" not in u
    r = await client.post("/v1/users", json=body({"name": "N", "surname": "S", "email": "new@x.io", "password": "pw"}), headers=h)
    ok_env(r, "115")
    r = await client.get(f"/v1/users/{u['id']}", headers=h)
    assert ok_env(r, "000")["data"]["user"]["id"] == u["id"]
    r = await client.patch(f"/v1/users/{u['id']}", json=body({"name": "Z"}), headers=h)
    d = ok_env(r, "002")["data"]["user"]
    assert d["name"] == "Z"
    r = await client.patch(f"/v1/users/{u['id']}", json=body({"name": "Z"}), headers=h)
    ok_env(r, "007")
    r = await client.delete(f"/v1/users/{u['id']}", headers=h)
    assert ok_env(r, "009")["data"] == {"user": {"id": u["id"]}}
    r = await client.get(f"/v1/users/{u['id']}", headers=h)
    assert r.status_code == 404
    ok_env(r, "215")


async def test_users_create_validation_and_perms(client, admin, user):
    r = await client.post("/v1/users", json=body({"name": "N"}), headers=admin[1])
    assert r.status_code == 422
    assert "email" in ok_env(r, "269")["error"]["fields"]
    r = await client.post("/v1/users", json={"name": "N"}, headers=admin[1])
    ok_env(r, "269")
    r = await client.post("/v1/users", json=body({"name": "N", "surname": "S", "email": "a@b.co", "password": "x"}), headers=user[1])
    assert r.status_code == 403
    ok_env(r, "186")


async def test_user_access_rules_and_patch_admin_fields(client, admin, user, mk_user):
    uid, h = user
    other = await mk_user("o@x.io")
    r = await client.get(f"/v1/users/{other}", headers=h)
    ok_env(r, "189")
    r = await client.patch(f"/v1/users/{uid}", json=body({"name": "Q", "role": "admin"}), headers=h)
    b = ok_env(r, "002")
    assert b["data"]["user"]["role"] == "user" and b["data"]["user"]["name"] == "Q"
    assert any("role" in w for w in b["metadata"]["warnings"])


async def test_users_list_settings_and_multisearch(client, admin, mk_user):
    for i in range(3):
        await mk_user(f"p{i}@x.io", name=f"n{i}")
    r = await client.get("/v1/users?limit=2&sort_by=id&order=desc", headers=admin[1])
    b = ok_env(r, "000")
    assert len(b["data"]["users"]) == 2
    s = b["data"]["settings"]
    assert s["total"] == 4 and s["limit"] == 2 and s["order"] == "desc" and s["sort_by"] == "id"
    r = await client.get(
        "/v1/users?search=n0,p0%40x.io&search_column=name,email&search_method=equals,equals",
        headers=admin[1],
    )
    assert [u["name"] for u in ok_env(r, "000")["data"]["users"]] == ["n0"]
    # v1.2 search_method accepts comparisons only; logical connectors use `filter`.
    r = await client.get(
        "/v1/users?search=n0,n1&search_column=name,name&search_method=equals,or", headers=admin[1]
    )
    ok_env(r, "066")
    r = await client.get("/v1/users?search=zzz&search_column=name", headers=admin[1])
    ok_env(r, "019")
    r = await client.get("/v1/users?search=x&search_column=password", headers=admin[1])
    ok_env(r, "127")
    r = await client.get("/v1/users?sort_by=password", headers=admin[1])
    ok_env(r, "125")
    r = await client.get("/v1/users?limit=100000", headers=admin[1])
    ok_env(r, "158")


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
    ok_env(r, "269")
    r = await client.get("/v1/users?search=b1@x.io&search_column=email", headers=h)
    ok_env(r, "019")  # atomic: nothing created
    r = await client.post("/v1/users/bulk", json={"data": {"users": [good, bad]}, "options": {"partial_allowed": True}}, headers=h)
    b = ok_env(r, "041")
    assert len(b["data"]["users"]["succeeded"]) == 1 and b["data"]["users"]["failed"][0]["id"] == 1
    r = await client.post("/v1/users/bulk", json={"data": {"users": [{**good, "email": "b2@x.io"}, {**good, "email": "b3@x.io"}]}}, headers=h)
    assert r.status_code == 201 and len(ok_env(r, "039")["data"]["users"]) == 2
    # duplicate email in partial mode -> failed element
    r = await client.post("/v1/users/bulk", json={"data": {"users": [{**good, "email": "b4@x.io"}, good]}, "options": {"partial_allowed": True}}, headers=h)
    b = ok_env(r, "041")
    assert b["data"]["users"]["failed"][0]["id"] == 1


async def test_users_bulk_get_patch_delete(client, admin, mk_user):
    h = admin[1]
    a, b_ = await mk_user("a@x.io"), await mk_user("b@x.io")
    r = await client.get(f"/v1/users/bulk/{a},{b_}", headers=h)
    b = ok_env(r, "000")
    assert [u["id"] for u in b["data"]["users"]] == [a, b_]
    assert "settings" not in b["data"]  # bulk GET is not a paginated collection list
    r = await client.get(f"/v1/users/bulk/{a},9999", headers=h)
    ok_env(r, "215")
    r = await client.get("/v1/users/bulk/a,b", headers=h)
    assert r.status_code == 422
    assert "ids" in ok_env(r, "269")["error"]["path"]
    r = await client.patch(
        f"/v1/users/bulk/{a},{b_}",
        json={"data": {"users": [{"name": "Bulk"}, {"name": "Bulk"}]}},
        headers=h,
    )
    assert [u["name"] for u in ok_env(r, "002")["data"]["users"]] == ["Bulk", "Bulk"]
    r = await client.patch(
        f"/v1/users/bulk/{a},9999",
        json={"data": {"users": [{"name": "X"}, {"name": "X"}]}},
        headers=h,
    )
    ok_env(r, "215")
    r = await client.get(f"/v1/users/{a}", headers=h)
    assert r.json()["data"]["user"]["name"] == "Bulk"  # atomic: unchanged by failed request
    r = await client.patch(
        f"/v1/users/bulk/{a},9999",
        json={
            "data": {"users": [{"name": "X"}, {"name": "X"}]},
            "options": {"partial_allowed": True},
        },
        headers=h,
    )
    b = ok_env(r, "004")
    assert b["data"]["users"]["failed"][0]["id"] == 9999
    r = await client.delete(f"/v1/users/bulk/{a},9999", headers=h)
    ok_env(r, "215")
    r = await client.delete(f"/v1/users/bulk/{a},9999?partial_allowed=true", headers=h)
    b = ok_env(r, "012")
    assert b["data"]["users"]["succeeded"] == [{"id": a}]
    r = await client.delete(f"/v1/users/bulk/{b_}", headers=h)
    assert ok_env(r, "009")["data"]["users"] == [{"id": b_}]


async def test_options_users(client):
    r = await client.options("/v1/users")
    assert "GET" in r.headers["allow"] and "POST" in r.headers["allow"]
    fns = r.headers["cursed-supported-functions"].split(",")
    assert {"GET.limit", "GET.offset", "GET.sort_by", "GET.search", "GET.search_method",
            "GET.search_column", "GET.multi_search"} <= set(fns)
    r = await client.options("/v1/users/bulk")
    assert r.headers["cursed-bulk"] == "on" and r.headers["cursed-max-bulk"] == "100"
    assert "POST.bulk" in r.headers["cursed-supported-functions"].split(",")
    r = await client.options("/v1/users/bulk/1,2")
    assert r.headers["cursed-bulk"] == "on"
    assert "GET.bulk" in r.headers["cursed-supported-functions"].split(",")


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
    ok_env(r, "215")
    r = await client.get(f"/v1/users/{admin[0]}/sessions", headers=h)
    ok_env(r, "189")
    r = await client.get("/v1/users/9999/sessions", headers=admin[1])
    ok_env(r, "215")
    # bulk
    r = await client.get(f"/v1/users/{uid}/sessions/bulk/{sid}", headers=h)
    b = ok_env(r, "000")
    assert [session["id"] for session in b["data"]["sessions"]] == [sid]
    assert "settings" not in b["data"]
    r = await client.delete(f"/v1/users/{uid}/sessions/bulk/{sid},999", headers=h)
    ok_env(r, "215")
    r = await client.delete(f"/v1/users/{uid}/sessions/bulk/{sid},999?partial_allowed=true", headers=h)
    b = ok_env(r, "012")
    assert b["data"]["sessions"]["succeeded"] == [{"id": sid}]
    # Revoking the current session immediately invalidates the bearer on every route.
    r = await client.delete(f"/v1/users/{uid}/sessions/{sid}", headers=h)
    ok_env(r, "167")


async def test_session_delete_single(client, user):
    uid, h = user
    sid = (await client.get(f"/v1/users/{uid}/sessions", headers=h)).json()["data"]["sessions"][0]["id"]
    r = await client.delete(f"/v1/users/{uid}/sessions/{sid}", headers=h)
    assert ok_env(r, "009")["data"] == {"session": {"id": sid}}


async def test_generated_session_list_filter_access_and_schema(client, admin, user):
    uid, h = user
    path = f"/v1/users/{uid}/sessions"
    admin_path = f"/v1/users/{admin[0]}/sessions"
    sid = ok_env(await client.get(path, headers=h), "000")["data"]["sessions"][0]["id"]

    result = await client.options(path, headers=h)
    assert "GET.filter" in result.headers["cursed-supported-functions"].split(",")
    schema_url = result.headers["cursed-schema-url"]
    method = ok_env(await client.get(schema_url, headers=h), "000")["data"]["schema"]["methods"]["GET"]
    fields = method["query"]["filter"]["fields"]
    assert set(fields) == {"id", "uid", "last_login", "last_seen", "added_at", "updated_at"}
    assert all(field["type"] == "int" for field in fields.values())
    response = next(variant for variant in method["responses"]["200"] if variant["code"] == "000")
    response_schema = response["body_schema"]
    assert response_schema["properties"]["code"]["const"] == "000"
    assert "token_hash" not in str(response_schema)
    ok_env(await client.get(schema_url), "177")
    for response in (await client.options(path), await client.options(admin_path, headers=h)):
        assert "GET.filter" not in response.headers["cursed-supported-functions"].split(",")
        assert "cursed-schema-url" not in response.headers

    def encoded(expr):
        return urlsafe_b64encode(expr.encode()).decode().rstrip("=")

    # The owner scope remains in effect even when the filter includes another user.
    filter_ = encoded(f"id == {sid} OR uid == {admin[0]}")
    result = await client.get(path, params={"filter": filter_, "search": "pytest", "search_column": "ua"}, headers=h)
    data = ok_env(result, "000")["data"]
    assert [session["id"] for session in data["sessions"]] == [sid]
    assert data["settings"]["total"] == 1 and data["settings"]["filter"] == filter_
    assert data["settings"]["search_method"] == "include"
    ok_env(await client.get(admin_path, params={"filter": filter_}, headers=h), "189")

    result = await client.get(path, params={"filter": encoded("token_hash IS NULL")}, headers=h)
    assert "filter" in ok_env(result, "066")["error"]["query"]


async def test_session_delete_contract_describes_optional_request_and_partial_result(client, user):
    uid, h = user
    path = f"/v1/users/{uid}/sessions/bulk/1,2"
    options = await client.options(path, headers=h)
    schema_url = options.headers["cursed-schema-url"]
    methods = ok_env(await client.get(schema_url, headers=h), "000")["data"]["schema"]["methods"]
    delete_ = methods["DELETE"]
    assert delete_["request"]["required"] is False
    assert delete_["request"]["body_schema"]["required"] == ["data"]
    variants = {variant["code"]: variant for rows in delete_["responses"].values() for variant in rows}
    assert {"009", "012", "215", "269"} <= variants.keys()
    assert "succeeded" in str(variants["012"]["body_schema"])
    assert "failed" in str(variants["012"]["body_schema"])


async def test_generated_session_list_deletes_expired_sessions(client, user, sessionmaker_):
    from app.deps.auth import IDLE_SECONDS
    from app.models.session import Session

    uid, h = user
    timestamp = int(time.time()) - IDLE_SECONDS - 1
    async with sessionmaker_() as db:
        old = Session(uid=uid, ua="expired", ip="127.0.0.1", last_login=timestamp,
                      last_seen=timestamp, added_at=timestamp, updated_at=timestamp)
        db.add(old)
        await db.commit()
        old_id = old.id

    result = await client.get(f"/v1/users/{uid}/sessions", headers=h)
    assert ok_env(result, "000")["data"]["settings"]["total"] == 1
    async with sessionmaker_() as db:
        assert await db.get(Session, old_id) is None
