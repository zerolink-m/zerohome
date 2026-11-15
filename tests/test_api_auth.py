import hashlib
import time

import pytest
from sqlalchemy import select, update

from app.core.codes import BAD_CREDENTIALS, EMAIL_TAKEN, ENDPOINT_RATE_LIMITED, LOGIN_RATE_LIMITED, TOKEN_ERROR, TOKEN_MISSING
import jwt
from app.core.config import settings
from app.core.security import bearer_hash, create_session_token
from app.models.session import Session
from tests.conftest import bearer, login_access


def reg(user):
    return {"data": {"user": user}}


def cred(email, password):
    return {"data": {"credentials": {"email": email, "password": password}}}


async def test_register_returns_token_and_persists_only_hash(client, db):
    p = {"name": "A", "surname": "B", "email": "A@Example.com", "password": "pw"}
    r = await client.post("/v1/auth/register", json=reg(p))
    assert r.status_code == 201 and r.json()["code"] == "001"
    token = r.json()["data"]["token"]
    assert len(token) == 43 and isinstance(r.json()["data"]["user"]["id"], int)
    async with db() as s:
        row = (await s.scalars(select(Session))).one()
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert row.last_seen == row.added_at
    assert (await client.get("/v1/cams", headers=bearer(token))).status_code == 200
    assert (await client.post("/v1/auth/register", json=reg({**p, "email": "a@example.com"}))).json()["code"] == EMAIL_TAKEN


async def test_login_token_logout_and_revoke(client, mk_user, db):
    await mk_user("u@x.io")
    token = await login_access(client, "u@x.io")
    assert (await client.get("/v1/cams", headers=bearer(token))).status_code == 200
    async with db() as s:
        row = (await s.scalars(select(Session))).one()
        assert row.token_hash == bearer_hash(token)
        sid = row.id
    r = await client.post("/v1/auth/logout", headers=bearer(token))
    assert r.json()["code"] == "018" and r.json()["data"]["session"]["id"] == sid
    assert (await client.get("/v1/cams", headers=bearer(token))).status_code == 401
    async with db() as s:
        assert await s.get(Session, sid) is None


@pytest.mark.parametrize("email,pw", [("u@x.io", "bad"), ("nobody@x.io", "Passw0rd!")])
async def test_login_bad_credentials(client, mk_user, email, pw):
    await mk_user("u@x.io")
    assert (await client.post("/v1/auth/login", json=cred(email, pw))).json()["code"] == BAD_CREDENTIALS


async def test_login_rate_limit(client, mk_user):
    await mk_user("u@x.io")
    codes = [(await client.post("/v1/auth/login", json=cred("u@x.io", "bad"))).json()["code"] for _ in range(6)]
    assert codes[:5] == [BAD_CREDENTIALS] * 5 and codes[5] == LOGIN_RATE_LIMITED


async def test_register_rate_limit(client):
    codes = [(await client.post("/v1/auth/register", json=reg({
        "name": "A", "surname": "B", "email": f"u{i}@x.io", "password": "pw"}))).json()["code"] for i in range(4)]
    assert codes == ["001"] * 3 + [ENDPOINT_RATE_LIMITED]


async def test_legacy_refresh_one_time_conversion(client, mk_user, db):
    uid = await mk_user("u@x.io")
    now = int(time.time())
    async with db() as s:
        row = Session(uid=uid, ua="pytest-UA/1.0", ip="127.0.0.1", last_login=now,
                      last_seen=now, added_at=now, updated_at=now, token_hash=None)
        s.add(row)
        await s.commit()
        legacy = create_session_token(uid, row.id)
    assert (await client.get("/v1/cams", headers=bearer(legacy))).status_code == 401
    converted = await client.post("/v1/auth/new-refresh", headers=bearer(legacy))
    assert converted.json()["code"] == "019"
    token = converted.json()["data"]["token"]
    assert (await client.get("/v1/cams", headers=bearer(token))).status_code == 200
    assert (await client.post("/v1/auth/new-refresh", headers=bearer(legacy))).status_code == 401
    async with db() as s:
        assert (await s.get(Session, row.id)).token_hash == bearer_hash(token)
    assert (await client.post("/v1/auth/access", headers=bearer(legacy))).status_code == 404


async def test_expired_or_revoked_legacy_refresh_never_converts(client, mk_user, db):
    uid = await mk_user("u@x.io")
    now = int(time.time())
    async with db() as s:
        row = Session(uid=uid, ua="pytest-UA/1.0", ip="127.0.0.1", last_login=now,
                      last_seen=now, added_at=now, updated_at=now, token_hash=None)
        s.add(row)
        await s.commit()
        sid = row.id
    expired = jwt.encode({"uid": uid, "sid": sid, "iat": now - 86400, "exp": now - 1},
                         settings.JWT_SECRET_SESSION, algorithm="HS256")
    assert (await client.post("/v1/auth/new-refresh", headers=bearer(expired))).json()["code"] == "043"
    async with db() as s:
        assert (await s.get(Session, sid)).token_hash is None
        await s.delete(await s.get(Session, sid))
        await s.commit()
    assert (await client.post("/v1/auth/new-refresh", headers=bearer(create_session_token(uid, sid)))).json()["code"] == TOKEN_ERROR


async def test_old_access_is_rejected_even_with_live_session(client, mk_user):
    uid = await mk_user("u@x.io")
    await login_access(client, "u@x.io")
    now = int(time.time())
    legacy = jwt.encode({"uid": uid, "sid": 1, "role": "user", "iat": now, "exp": now + 300},
                        settings.JWT_SECRET_ACCESS, algorithm="HS256")
    assert (await client.get("/v1/cams", headers=bearer(legacy))).status_code == 401
    assert (await client.post("/v1/auth/new-refresh", headers=bearer(legacy))).status_code == 401


async def test_ua_mismatch_revokes_and_version_change_keeps_session(client, mk_user):
    await mk_user("u@x.io")
    r = await client.post("/v1/auth/login", json=cred("u@x.io", "Passw0rd!"),
                          headers={"user-agent": "Browser/100.0.1"})
    token = r.json()["data"]["token"]
    assert (await client.get("/v1/cams", headers={**bearer(token), "user-agent": "Browser/101.0.2"})).status_code == 200
    assert (await client.get("/v1/cams", headers={**bearer(token), "user-agent": "Intruder"})).status_code == 401
    assert (await client.get("/v1/cams", headers={**bearer(token), "user-agent": "Browser/101.0.2"})).status_code == 401


async def test_idle_expiry_deletes_real_row(client, mk_user, db):
    await mk_user("u@x.io")
    token = await login_access(client, "u@x.io")
    async with db() as s:
        await s.execute(update(Session).values(last_seen=int(time.time()) - 30 * 86400))
        await s.commit()
    assert (await client.get("/v1/cams", headers=bearer(token))).status_code == 401
    async with db() as s:
        assert (await s.scalars(select(Session))).all() == []


async def test_missing_token(client):
    r = await client.post("/v1/auth/logout")
    assert r.json()["code"] == TOKEN_MISSING and "no-store" in r.headers["cache-control"]


async def test_options_auth_login(client):
    r = await client.options("/v1/auth/login")
    assert r.status_code in (200, 204) and "POST" in r.headers["allow"]
