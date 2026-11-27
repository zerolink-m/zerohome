import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base

from app.core.config import Settings
from app.services.cluster import leases
from app.services.cluster.identity import get_node_id, local_capabilities

TTL = 30


async def acq(sm, node, now, fn="ingest", rid=1):
    async with sm() as db:
        return await leases.try_acquire(db, fn, rid, node, TTL, now)


async def test_acquire_free(sessionmaker_):
    lease = await acq(sessionmaker_, "a", 100)
    assert (lease.owner, lease.generation, lease.expires_at) == ("a", 1, 130)


async def test_same_node_extends_without_generation_change(sessionmaker_):
    await acq(sessionmaker_, "a", 100)
    lease = await acq(sessionmaker_, "a", 110)
    assert lease.generation == 1 and lease.expires_at == 140


async def test_other_node_blocked_until_expiry_then_takes_over(sessionmaker_):
    await acq(sessionmaker_, "a", 100)
    assert await acq(sessionmaker_, "b", 129) is None
    lease = await acq(sessionmaker_, "b", 130)
    assert lease.owner == "b" and lease.generation == 2


async def test_renew_and_lost_rights(sessionmaker_):
    old = await acq(sessionmaker_, "a", 100)
    async with sessionmaker_() as db:
        assert await leases.renew(db, old, TTL, 110) is True
    new = await acq(sessionmaker_, "b", 200)
    async with sessionmaker_() as db:
        assert await leases.renew(db, old, TTL, 201) is False  # stale generation/owner
        assert await leases.renew(db, new, TTL, 201) is True


async def test_renew_after_expiry_fails(sessionmaker_):
    lease = await acq(sessionmaker_, "a", 100)
    async with sessionmaker_() as db:
        assert await leases.renew(db, lease, TTL, 130) is False


async def test_old_generation_same_node_cannot_renew(sessionmaker_):
    old = await acq(sessionmaker_, "a", 100)
    new = await acq(sessionmaker_, "a", 200)  # expired -> new generation
    assert new.generation == 2
    async with sessionmaker_() as db:
        assert await leases.renew(db, old, TTL, 201) is False


async def test_release_keeps_generation(sessionmaker_):
    lease = await acq(sessionmaker_, "a", 100)
    async with sessionmaker_() as db:
        await leases.release(db, lease)
    async with sessionmaker_() as db:
        rows = await leases.list_assignments(db)
    assert rows[0].owner_node_id is None and rows[0].generation == 1 and rows[0].expires_at == 0
    taken = await acq(sessionmaker_, "b", 101)
    assert taken.generation == 2
    async with sessionmaker_() as db:  # stale release is a no-op
        await leases.release(db, lease)
        assert (await leases.list_assignments(db))[0].owner_node_id == "b"


@pytest_asyncio.fixture
async def file_sm(tmp_path):
    # Separate connections per session (the shared StaticPool connection would merge transactions).
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}", connect_args={"timeout": 30})
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(eng, expire_on_commit=False)
    await eng.dispose()


async def test_concurrent_acquire_single_winner(file_sm):
    res = await asyncio.gather(*(acq(file_sm, n, 100, rid=7) for n in "abcde"))
    assert len([r for r in res if r]) == 1


async def test_concurrent_takeover_single_winner(file_sm):
    await acq(file_sm, "a", 100, rid=7)
    res = await asyncio.gather(*(acq(file_sm, n, 500, rid=7) for n in "bcde"))
    winners = [r for r in res if r]
    assert len(winners) == 1 and winners[0].generation == 2


async def test_nodes(sessionmaker_):
    async with sessionmaker_() as db:
        await leases.register_node(db, "a", {"ingest": True}, "10.0.0.1", "v1", 100)
        await leases.register_node(db, "b", {}, None, "v1", 50)
        await leases.heartbeat(db, "a", {"ingest": False}, "10.0.0.1", "v1", 120)
        alive = await leases.list_nodes(db, 125, 30)
    assert [n.node_id for n in alive] == ["a"]
    assert alive[0].capabilities == {"ingest": False} and alive[0].started_at == 100


async def test_tables_created(engine):
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: inspect(c).get_table_names())
    assert {"nodes", "assignments"} <= set(names)


def test_migration_chain():
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    script = ScriptDirectory.from_config(config)
    rev = script.get_revision("47500_cluster_leases")
    assert rev.down_revision == "47400_session_bearer"
    assert len(rev.revision) <= 32
    nxt = script.get_revision("47600_notify_photo")
    assert nxt.down_revision == "47500_cluster_leases"
    assert len(nxt.revision) <= 32
    holder = script.get_revision("47700_lease_holder")
    assert holder.down_revision == "47600_notify_photo"
    assert len(holder.revision) <= 32
    names = script.get_revision("47800_schema_names")
    assert names.down_revision == "47700_lease_holder"
    assert len(names.revision) <= 32
    runtime = script.get_revision("47900_runtime_configuration")
    assert runtime.down_revision == "47800_schema_names"
    assert len(runtime.revision) <= 32
    nullable = script.get_revision("48000_user_nullable_settings")
    assert nullable.down_revision == "47900_runtime_configuration"
    assert len(nullable.revision) <= 32
    for revision, previous in (
        ("48100_connection_resources", "48000_user_nullable_settings"),
        ("48200_camera_resource", "48100_connection_resources"),
        ("48300_connection_revision", "48200_camera_resource"),
        ("48400_local_connections", "48300_connection_revision"),
        ("48500_motion_cooldown", "48400_local_connections"),
    ):
        current = script.get_revision(revision)
        assert current.down_revision == previous
        assert len(current.revision) <= 32
    assert script.get_heads() == ["48500_motion_cooldown"]


def _settings(**kw):
    return Settings(_env_file=None, **kw)


def test_lease_timing_validation():
    _settings()
    with pytest.raises(ValidationError):
        _settings(LEASE_TTL_SECONDS=15, LEASE_RENEW_INTERVAL_SECONDS=10)


def test_cluster_allows_file_backed_node_identity():
    assert _settings(CLUSTER_ENABLED=True).CLUSTER_ENABLED is True


def test_cluster_and_standalone_settings():
    assert _settings(CLUSTER_ENABLED=True).CLUSTER_ENABLED is True
    assert _settings(CLUSTER_ENABLED=False).CLUSTER_ENABLED is False


def test_node_id_and_capabilities(tmp_path, monkeypatch):
    from app.services.cluster import identity

    monkeypatch.setattr(identity, "IDENTITY_FILE", tmp_path / "identity" / "node-id")
    monkeypatch.setattr(identity, "_generated_id", None)
    s = _settings()
    a = get_node_id()
    assert a and a == get_node_id() and len(a) <= 64
    caps = local_capabilities(_settings(INGEST_ENABLED=True, MOTION_DETECTION_ENABLED=False))
    assert caps["ingest"] is True and caps["motion"] is False and caps["notify"] is True
