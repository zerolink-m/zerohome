"""Test bootstrap.

Env vars are set BEFORE `app` is imported so that `app.core.config.settings`
never uses the real .env values (environment wins over the .env file in
pydantic-settings). The DB is an in-memory SQLite (aiosqlite); `get_db` and every
`AsyncSessionLocal` used by request handlers are replaced. Lifespan is never run
(httpx.ASGITransport does not trigger it), so no background workers start.
"""
import os

_TEST_ENV = {
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "1",
    "DB_USER": "test",
    "DB_PASSWORD": "test",
    "DB_NAME": "test_never_connect",
    "JWT_SECRET_SESSION": "test-secret-session-0123456789abcdef0123",
    "JWT_SECRET_ACCESS": "test-secret-access-0123456789abcdef01234",
    "JWT_SECRET_ACCESS_ADMIN": "test-secret-admin-0123456789abcdef01234",
    "JWT_SECRET_SHARE": "test-secret-share-0123456789abcdef012345",
    "JWT_ALGORITHM": "HS256",
    "ACCESS_TOKEN_TTL_SECONDS": "900",
    "SESSION_TOKEN_TTL_SECONDS": "86400",
    "SHARE_TOKEN_MIN_TTL_SECONDS": "60",
    "SHARE_TOKEN_MAX_TTL_SECONDS": "3600",
    "HLS_ENABLED": "false",
    "WEBRTC_ENABLED": "false",
}
os.environ.update(_TEST_ENV)

import time  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core import rate_limit as rl  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401  (register all tables)
from app.db import session as db_session  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.cams import Cam  # noqa: E402
from app.models.user import User  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    rl._buckets.clear()
    yield
    rl._buckets.clear()


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker_(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def app(sessionmaker_, monkeypatch):
    application = create_app()

    async def _get_db():
        async with sessionmaker_() as s:
            yield s

    application.dependency_overrides[get_db] = _get_db
    # background tasks inside routers open their own sessions
    monkeypatch.setattr(db_session, "AsyncSessionLocal", sessionmaker_)
    monkeypatch.setattr("app.routers.auth.AsyncSessionLocal", sessionmaker_)
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 12345))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"user-agent": "pytest-UA/1.0"}
    ) as c:
        yield c


@pytest.fixture
def db(sessionmaker_):
    return sessionmaker_


async def make_user(sm, email, password="Passw0rd!", role="user", name="N", surname="S"):
    now = int(time.time())
    async with sm() as s:
        u = User(
            name=name, surname=surname, email=email,
            password=await hash_password(password), role=role,
            added_at=now, updated_at=now,
        )
        s.add(u)
        await s.commit()
        return u.id


async def make_cam(sm, owner, enabled=1, control_proto=None):
    now = int(time.time())
    async with sm() as s:
        c = Cam(
            ownuser=owner, name="cam", enabled=enabled, proto="rtsp", addr="10.0.0.5",
            port=554, path="", username="u", password="p",
            control_proto=control_proto, added_at=now, updated_at=now,
        )
        s.add(c)
        await s.commit()
        return c.id


@pytest.fixture
def mk_user(sessionmaker_):
    async def _f(*a, **kw):
        return await make_user(sessionmaker_, *a, **kw)
    return _f


@pytest.fixture
def mk_cam(sessionmaker_):
    async def _f(*a, **kw):
        return await make_cam(sessionmaker_, *a, **kw)
    return _f


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def login_access(client, email, password="Passw0rd!"):
    """Full login -> session -> access flow; returns access token."""
    r = await client.post("/v1/auth/login", json={"data": {"credentials": {"email": email, "password": password}}})
    assert r.json()["code"] == "010", r.text
    r = await client.post("/v1/auth/access", headers=bearer(r.json()["data"]["token"]))
    assert r.json()["code"] == "019", r.text
    return r.json()["data"]["token"]
