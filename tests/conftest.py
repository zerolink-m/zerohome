"""Test bootstrap.

Env vars are set BEFORE `app` is imported so that `app.core.config.settings`
never uses the real .env values (environment wins over the .env file in
pydantic-settings). The DB is an in-memory SQLite (aiosqlite); `get_db` and every
`AsyncSessionLocal` used by request handlers are replaced. `httpx.ASGITransport`
does not trigger lifespan; tests that enter it explicitly stub runtime hydration
and background workers.
"""
import os

_TEST_ENV = {
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "1",
    "DB_USER": "test",
    "DB_PASSWORD": "test",
    "DB_NAME": "test_never_connect",
    # One-time test bootstrap values. Runtime signing keys are loaded from the
    # database by production; tests provide these explicitly for DB seeding.
    "BOOTSTRAP_JWT_SECRET_SESSION": "test-secret-session-0123456789abcdef0123",
    "BOOTSTRAP_JWT_SECRET_SHARE": "test-secret-share-0123456789abcdef012345",
}
os.environ.update(_TEST_ENV)

import time  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core import rate_limit as rl  # noqa: E402
from app.core.security import bearer_hash, hash_password, new_bearer_token  # noqa: E402
from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401  (register all tables)
from app.db import session as db_session  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.cams import Cam  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.session import Session  # noqa: E402
from app.services.cluster import identity as node_identity  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    rl._buckets.clear()
    yield
    rl._buckets.clear()


@pytest.fixture(autouse=True)
def _reset_session_activity():
    from app.services import session_activity
    session_activity.reset()
    yield
    session_activity.reset()


@pytest.fixture(autouse=True)
def _isolate_node_identity(monkeypatch, tmp_path):
    """Use a per-test node-id file; never write identity under the host path."""
    monkeypatch.setattr(node_identity, "IDENTITY_FILE", tmp_path / "node-id")
    monkeypatch.setattr(node_identity, "_generated_id", None)
    monkeypatch.setattr(node_identity, "_identity_pending_registration", False)


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


_active_test_sessionmaker = None


@pytest.fixture
def app(sessionmaker_, monkeypatch):
    global _active_test_sessionmaker
    _active_test_sessionmaker = sessionmaker_
    application = create_app()

    async def _get_db():
        async with sessionmaker_() as s:
            yield s

    application.dependency_overrides[get_db] = _get_db
    monkeypatch.setattr(db_session, "AsyncSessionLocal", sessionmaker_)
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


async def mint_test_token(uid, role="user"):
    """Create a real database-backed session for integration tests (not an app API)."""
    assert _active_test_sessionmaker is not None
    token = new_bearer_token()
    now = int(time.time())
    async with _active_test_sessionmaker() as db:
        user = await db.get(User, uid)
        assert user is not None and user.role == role
        db.add(Session(uid=uid, ua="pytest-UA/1.0", ip="127.0.0.1", last_login=now,
                       last_seen=now, added_at=now, updated_at=now, token_hash=bearer_hash(token)))
        await db.commit()
    return token


async def login_access(client, email, password="Passw0rd!"):
    """Log in and return the database-backed bearer token."""
    r = await client.post("/v1/auth/login", json={"data": {"credentials": {"email": email, "password": password}}})
    assert r.json()["code"] == "010", r.text
    return r.json()["data"]["token"]
