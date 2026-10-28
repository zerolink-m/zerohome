import pytest
from sqlalchemy import select

from app.core.codes import (BAD_CREDENTIALS, BAD_REQUEST, EMAIL_TAKEN, FORBIDDEN_ROLE,
                            ENDPOINT_RATE_LIMITED, LOGIN_RATE_LIMITED, TOKEN_ERROR, TOKEN_MISSING)
from app.core.security import create_access_token, create_session_token
from app.models.session import Session as SessionModel
from tests.conftest import bearer, login_access


def assert_envelope(body, code):
    assert set(body) == {"code", "code_detail", "error", "metadata", "data"}
    assert body["code"] == code
    assert isinstance(body["code_detail"], str) and body["metadata"]["request_id"]
    if code in ("000", "001", "007", "010", "018", "019"):
        assert body["error"] == {}
    else:
        assert isinstance(body["error"]["error"], str) and body["data"] == {}


def reg(user):
    return {"data": {"user": user}}


def cred(email, password):
    return {"data": {"credentials": {"email": email, "password": password}}}


async def test_register_ok_and_duplicate(client):
    p = {"name": "A", "surname": "B", "email": "A@Example.com", "password": "pw"}
    r = await client.post("/v1/auth/register", json=reg(p))
    assert r.status_code == 201
    assert_envelope(r.json(), "001")
    assert isinstance(r.json()["data"]["user"]["id"], int)
    r = await client.post("/v1/auth/register", json=reg({**p, "email": "a@example.com"}))
    assert_envelope(r.json(), EMAIL_TAKEN)  # email is lower-cased


async def test_register_invalid_body(client):
    r = await client.post("/v1/auth/register", json=reg({"name": "A", "email": "nope"}))
    assert r.status_code == 422
    assert_envelope(r.json(), "109")
    assert "email" in r.json()["error"]["fields"]
    r = await client.post("/v1/auth/register", json=reg({"name": "A", "surname": "B",
                          "email": "a@b.co", "password": "x", "extra": 1}))
    assert_envelope(r.json(), "109")  # StrictModel forbids extras
    assert r.json()["error"]["fields"]["extra"]["unknown"] is True


async def test_register_rate_limited(client):
    codes = []
    for i in range(4):
        r = await client.post("/v1/auth/register", json=reg({
            "name": "A", "surname": "B", "email": f"u{i}@x.io", "password": "pw"}))
        codes.append(r.json()["code"])
    assert codes == ["001", "001", "001", ENDPOINT_RATE_LIMITED]
    assert "retry-after" in r.headers


async def test_login_ok_returns_session_token_and_creates_session(client, mk_user, db):
    uid = await mk_user("u@x.io")
    r = await client.post("/v1/auth/login", json=cred("U@X.io", "Passw0rd!"))
    assert r.status_code == 200
    assert_envelope(r.json(), "010")
    tok = r.json()["data"]["token"]
    from app.core.security import decode_any
    p = decode_any(tok)
    assert p.kind == "session" and p.uid == uid
    async with db() as s:
        rows = (await s.scalars(select(SessionModel))).all()
    assert len(rows) == 1 and rows[0].ua == "pytest-UA/1.0" and rows[0].id == p.sid


@pytest.mark.parametrize("email,pw", [("u@x.io", "bad"), ("nobody@x.io", "Passw0rd!")])
async def test_login_bad_credentials(client, mk_user, email, pw):
    await mk_user("u@x.io")
    r = await client.post("/v1/auth/login", json=cred(email, pw))
    assert_envelope(r.json(), BAD_CREDENTIALS)


async def test_login_rate_limited_after_5(client, mk_user):
    await mk_user("u@x.io")
    codes = [(await client.post("/v1/auth/login", json=cred("u@x.io", "bad"))).json()["code"]
             for _ in range(6)]
    assert codes[:5] == [BAD_CREDENTIALS] * 5 and codes[5] == LOGIN_RATE_LIMITED


async def test_access_exchange_ok_user_and_admin(client, mk_user):
    await mk_user("u@x.io")
    await mk_user("a@x.io", role="admin")
    from app.core.security import decode_any
    assert decode_any(await login_access(client, "u@x.io")).kind == "access"
    assert decode_any(await login_access(client, "a@x.io")).kind == "access-admin"


async def test_access_requires_session_token(client, mk_user):
    uid = await mk_user("u@x.io")
    for h, code in (({}, TOKEN_MISSING), (bearer("junk"), "149"),
                    (bearer(create_access_token(uid, 1, "user")), TOKEN_ERROR)):
        r = await client.post("/v1/auth/access", headers=h)
        assert_envelope(r.json(), code)


async def test_access_unknown_session(client, mk_user):
    uid = await mk_user("u@x.io")
    r = await client.post("/v1/auth/access", headers=bearer(create_session_token(uid, 999)))
    assert_envelope(r.json(), TOKEN_ERROR)


async def test_access_ua_mismatch_deletes_session(client, mk_user, db):
    await mk_user("u@x.io")
    r = await client.post("/v1/auth/login", json=cred("u@x.io", "Passw0rd!"))
    tok = r.json()["data"]["token"]
    r = await client.post("/v1/auth/access", headers={**bearer(tok), "user-agent": "Totally Different"})
    assert_envelope(r.json(), TOKEN_ERROR)
    async with db() as s:
        assert (await s.scalars(select(SessionModel))).all() == []
    # session gone -> even the original UA no longer works
    r = await client.post("/v1/auth/access", headers=bearer(tok))
    assert_envelope(r.json(), TOKEN_ERROR)


async def test_access_ua_version_bump_updates_ua(client, mk_user, db):
    await mk_user("u@x.io")
    r = await client.post("/v1/auth/login", json=cred("u@x.io", "Passw0rd!"),
                          headers={"user-agent": "Browser/100.0.1"})
    tok = r.json()["data"]["token"]
    r = await client.post("/v1/auth/access", headers={**bearer(tok), "user-agent": "Browser/101.0.2"})
    assert_envelope(r.json(), "019")
    async with db() as s:
        row = (await s.scalars(select(SessionModel))).one()
    assert row.ua == "Browser/101.0.2"


async def test_logout_deletes_session(client, mk_user, db):
    await mk_user("u@x.io")
    acc = await login_access(client, "u@x.io")
    r = await client.post("/v1/auth/logout", headers=bearer(acc))
    assert_envelope(r.json(), "018")
    assert isinstance(r.json()["data"]["session"]["id"], int)
    async with db() as s:
        assert (await s.scalars(select(SessionModel))).all() == []


async def test_logout_requires_access_token(client, mk_user):
    uid = await mk_user("u@x.io")
    r = await client.post("/v1/auth/logout", headers=bearer(create_session_token(uid, 1)))
    assert_envelope(r.json(), TOKEN_ERROR)
    r = await client.post("/v1/auth/logout")
    assert_envelope(r.json(), TOKEN_MISSING)


async def test_cache_headers_default(client):
    r = await client.post("/v1/auth/logout")
    assert "no-store" in r.headers["cache-control"]


# --- documented contract vs. actual HTTP statuses (API.md: code -> HTTP status) ---
@pytest.mark.parametrize("path,expected", [("/v1/auth/logout", 401)])
async def test_http_status_matches_documented_code(client, path, expected):
    r = await client.post(path)
    assert r.status_code == expected
    assert r.json()["code"] == "046"


# --- Request object contract ---
async def test_register_old_flat_body_rejected(client):
    r = await client.post("/v1/auth/register", json={"name": "A", "surname": "B", "email": "a@b.co", "password": "x"})
    assert r.status_code == 422
    assert_envelope(r.json(), "109")
    assert "data" in r.json()["error"]["fields"]


async def test_register_missing_key_and_empty_body_and_bad_json(client):
    r = await client.post("/v1/auth/register", json={"data": {}})
    assert_envelope(r.json(), "109")
    assert "user" in r.json()["error"]["fields"]
    r = await client.post("/v1/auth/register", content=b"", headers={"content-type": "application/json"})
    assert_envelope(r.json(), "121")
    r = await client.post("/v1/auth/register", content=b"{bad", headers={"content-type": "application/json"})
    assert_envelope(r.json(), "117")


async def test_register_wrong_content_type(client):
    r = await client.post("/v1/auth/register", content=b"x", headers={"content-type": "text/plain"})
    assert_envelope(r.json(), "107")


async def test_login_validation_nested_field(client):
    r = await client.post("/v1/auth/login", json={"data": {"credentials": {"email": "bad", "password": "x"}}})
    assert r.status_code == 422
    assert_envelope(r.json(), "109")
    assert "email" in r.json()["error"]["fields"]


async def test_access_token_data_shape(client, mk_user):
    await mk_user("u@x.io")
    r = await client.post("/v1/auth/login", json=cred("u@x.io", "Passw0rd!"))
    assert set(r.json()["data"]) == {"token"}
    r = await client.post("/v1/auth/access", headers=bearer(r.json()["data"]["token"]))
    assert r.status_code == 200 and set(r.json()["data"]) == {"token"}


async def test_options_auth_login(client):
    r = await client.options("/v1/auth/login")
    assert r.status_code in (200, 204)
    assert "POST" in r.headers["allow"] and "OPTIONS" in r.headers["allow"]
    assert r.headers["cursed-bulk"] == "off"
