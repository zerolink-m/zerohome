"""Deferred sessions.last_seen refresh (app/services/session_activity.py)."""
import time

import pytest
from sqlalchemy import delete, select, update

from app.core.security import bearer_hash
from app.models.session import Session
from app.services import session_activity as sa
from tests.conftest import bearer, mint_test_token


async def _stale_session(mk_user, db, age=300):
    uid = await mk_user("act@example.com")
    token = await mint_test_token(uid)
    old = int(time.time()) - age
    async with db() as s:
        await s.execute(update(Session).values(last_seen=old))
        await s.commit()
    return token, old


async def _row(db):
    async with db() as s:
        return (await s.execute(select(Session))).scalars().one()


async def test_request_does_not_write_then_flush_updates(app, client, mk_user, db):
    token, old = await _stale_session(mk_user, db)
    r = await client.get("/v1/cams", headers=bearer(token))
    assert r.status_code == 200
    assert (await _row(db)).last_seen == old
    assert sa.pending_count() == 1
    await sa.flush()
    row = await _row(db)
    assert row.last_seen >= int(time.time()) - 5
    assert sa.pending_count() == 0


async def test_burst_produces_single_pending_entry_and_write(app, client, mk_user, db):
    token, old = await _stale_session(mk_user, db)
    for _ in range(10):
        assert (await client.get("/v1/cams", headers=bearer(token))).status_code == 200
    assert sa.pending_count() == 1
    await sa.flush()
    first = (await _row(db)).last_seen
    assert first > old
    # Already flushed within the interval: further requests do not re-enqueue.
    await client.get("/v1/cams", headers=bearer(token))
    assert sa.pending_count() == 0


async def test_flush_after_session_deleted_does_not_recreate(app, client, mk_user, db):
    token, _ = await _stale_session(mk_user, db)
    await client.get("/v1/cams", headers=bearer(token))
    assert sa.pending_count() == 1
    async with db() as s:
        await s.execute(delete(Session))
        await s.commit()
    await sa.flush()
    async with db() as s:
        assert (await s.execute(select(Session))).scalars().all() == []


async def test_flush_never_decreases_last_seen(app, mk_user, db):
    token, _ = await _stale_session(mk_user, db, age=10)
    row = await _row(db)
    sa.touch(row.id, bearer_hash(token), row.last_seen - 5)
    await sa.flush()
    assert (await _row(db)).last_seen == row.last_seen
    sa.reset()
    sa.touch(row.id, bearer_hash("other"), int(time.time()))  # wrong token hash
    await sa.flush()
    assert (await _row(db)).last_seen == row.last_seen


async def test_flush_failure_is_retried_and_worker_survives(app, mk_user, db, monkeypatch):
    from app.db import session as db_session
    token, old = await _stale_session(mk_user, db)
    row = await _row(db)
    sa.touch(row.id, bearer_hash(token), int(time.time()))

    class Broken:
        def __call__(self):
            raise RuntimeError("db down")

    good = db_session.AsyncSessionLocal
    monkeypatch.setattr(db_session, "AsyncSessionLocal", Broken())
    monkeypatch.setattr(sa, "FLUSH_INTERVAL", 0.05)
    sa.start()
    import asyncio
    await asyncio.sleep(0.2)
    assert sa._task is not None and not sa._task.done()
    assert sa.pending_count() == 1
    assert (await _row(db)).last_seen == old
    monkeypatch.setattr(db_session, "AsyncSessionLocal", good)
    await asyncio.sleep(0.3)
    assert sa.pending_count() == 0
    assert (await _row(db)).last_seen > old
    await sa.stop()


async def test_stop_flushes_pending_and_is_idempotent(app, mk_user, db):
    token, old = await _stale_session(mk_user, db)
    row = await _row(db)
    sa.start()
    sa.start()
    sa.touch(row.id, bearer_hash(token), int(time.time()))
    await sa.stop()
    await sa.stop()
    assert (await _row(db)).last_seen > old
    assert sa.pending_count() == 0


def test_touch_is_bounded_and_never_raises(monkeypatch):
    monkeypatch.setattr(sa, "MAX_ENTRIES", 3)
    now = int(time.time())
    for i in range(10):
        sa.touch(i, "h", now)
    assert sa.pending_count() == 3
    sa.touch(None, None, None)  # garbage must not raise
