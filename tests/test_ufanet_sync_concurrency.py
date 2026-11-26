import asyncio

import pytest
from sqlalchemy import select

from app.models.connection import Connection, ConnectionCredentials, ConnectionResource
from app.models.switch import Switch
from app.services.connection_sync import ufanet_sync


@pytest.mark.asyncio
async def test_older_sync_cannot_overwrite_newer_result(sessionmaker_, mk_user, monkeypatch):
    import ufanet_async

    owner = await mk_user("sync-race@example.org")
    async with sessionmaker_() as db:
        conn = Connection(uid=owner, provider="ufanet-smart-home", name="home", meta={}, enabled=1, added_at=1, updated_at=1)
        db.add(conn)
        await db.flush()
        connection_id = conn.id
        db.add(ConnectionCredentials(connection_id=connection_id, credentials={"contract": "1", "password": "x"}, updated_at=1))
        await db.commit()

    started, release = asyncio.Event(), asyncio.Event()
    count = 0

    class FakeClient:
        def __init__(self, config):
            self.ucams = self.smart_home = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def cameras(client):
        nonlocal count
        count += 1
        if count == 1:
            started.set()
            await release.wait()
            return [("old", "Old", {}, {})]
        return [("new", "New", {}, {})]

    async def skuds(client):
        return []

    monkeypatch.setattr(ufanet_async, "UfanetClient", FakeClient)
    monkeypatch.setattr(ufanet_sync, "_cameras", cameras)
    monkeypatch.setattr(ufanet_sync, "_skuds", skuds)

    async def run_sync():
        async with sessionmaker_() as db:
            await ufanet_sync.sync_ufanet(db, connection_id)

    old = asyncio.create_task(run_sync())
    await started.wait()
    await run_sync()
    release.set()
    await old
    async with sessionmaker_() as db:
        resources = (await db.execute(select(ConnectionResource))).scalars().all()
        assert [r.external_id for r in resources] == ["new"]


@pytest.mark.asyncio
async def test_disable_during_fetch_discards_result(sessionmaker_, mk_user, monkeypatch):
    import ufanet_async

    owner = await mk_user("sync-disable@example.org")
    async with sessionmaker_() as db:
        conn = Connection(uid=owner, provider="ufanet-smart-home", name="home", meta={}, enabled=1, added_at=1, updated_at=1)
        db.add(conn)
        await db.flush()
        connection_id = conn.id
        db.add(ConnectionCredentials(connection_id=connection_id, credentials={"contract": "1", "password": "x"}, updated_at=1))
        await db.commit()

    started, release = asyncio.Event(), asyncio.Event()

    class FakeClient:
        def __init__(self, config):
            self.ucams = self.smart_home = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def cameras(client):
        started.set()
        await release.wait()
        return [("1", "Camera", {}, {})]

    async def skuds(client):
        return []

    monkeypatch.setattr(ufanet_async, "UfanetClient", FakeClient)
    monkeypatch.setattr(ufanet_sync, "_cameras", cameras)
    monkeypatch.setattr(ufanet_sync, "_skuds", skuds)

    async def run_sync():
        async with sessionmaker_() as db:
            await ufanet_sync.sync_ufanet(db, connection_id)

    task = asyncio.create_task(run_sync())
    await started.wait()
    async with sessionmaker_() as db:
        conn = await db.get(Connection, connection_id)
        conn.enabled = 0
        conn.sync_revision += 1
        await db.commit()
    release.set()
    await task
    async with sessionmaker_() as db:
        assert (await db.execute(select(ConnectionResource))).scalars().all() == []


@pytest.mark.asyncio
async def test_ambiguous_empty_skud_catalog_fails_closed_and_recovers(sessionmaker_, mk_user, monkeypatch):
    import ufanet_async

    owner = await mk_user("skud-empty@example.org")
    async with sessionmaker_() as db:
        conn = Connection(uid=owner, provider="ufanet-smart-home", name="home", meta={}, enabled=1, added_at=1, updated_at=1)
        db.add(conn)
        await db.flush()
        cid = conn.id
        db.add(ConnectionCredentials(connection_id=cid, credentials={"contract": "1", "password": "x"}, updated_at=1))
        db.add(ConnectionResource(connection_id=cid, resource_type="skud", external_id="1", name="door", capabilities={"open": True}, attributes={}, available=1, first_seen_at=1, last_seen_at=1))
        await db.flush()
        from app.services.connection_sync.ufanet_skuds import materialize_ufanet_skuds
        switch = (await materialize_ufanet_skuds(db, cid))[0]
        sid = switch.id
        switch.enabled = 1
        await db.commit()

    class FakeClient:
        def __init__(self, config):
            self.ucams = self.smart_home = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def empty(client):
        return []

    monkeypatch.setattr(ufanet_async, "UfanetClient", FakeClient)
    monkeypatch.setattr(ufanet_sync, "_cameras", empty)
    monkeypatch.setattr(ufanet_sync, "_skuds", empty)
    async with sessionmaker_() as db:
        await ufanet_sync.sync_ufanet(db, cid)
    async with sessionmaker_() as db:
        resource = (await db.execute(select(ConnectionResource))).scalar_one()
        assert resource.available == 0
        assert (await db.get(Switch, sid)).enabled == 1
        assert "AmbiguousEmptyCatalog" in (await db.get(Connection, cid)).last_sync_error

    async def restored(client):
        return [("1", "door", {"open": True}, {})]

    monkeypatch.setattr(ufanet_sync, "_skuds", restored)
    async with sessionmaker_() as db:
        await ufanet_sync.sync_ufanet(db, cid)
    async with sessionmaker_() as db:
        resource = (await db.execute(select(ConnectionResource))).scalar_one()
        assert resource.available == 1
        assert (await db.get(Switch, sid)).enabled == 1
