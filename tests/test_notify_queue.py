"""Notification job claim: one winner, retry, and a stale generation cannot finish the job."""
import asyncio

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.notification import NotificationJob
from app.services import notify_queue


async def _enqueue(sm, message="hello", now=100, kind="manual"):
    async with sm() as db:
        job = await notify_queue.enqueue(db, kind=kind, message=message, now=now)
        await db.commit()
        return job.id


async def _row(sm, job_id):
    async with sm() as db:
        return (await db.execute(select(NotificationJob).where(NotificationJob.id == job_id))).scalar_one()


async def test_enqueue_and_claim_one_of_two(sessionmaker_):
    first = await _enqueue(sessionmaker_, "a")
    second = await _enqueue(sessionmaker_, "b")
    async with sessionmaker_() as db:
        claimed = await notify_queue.claim(db, "node-a", 100, 1, 30)
    assert len(claimed) == 1
    assert claimed[0].id == first
    assert claimed[0].state == "sending"
    assert claimed[0].claimed_by == "node-a"
    assert claimed[0].claimed_generation == 1
    assert claimed[0].attempts == 1
    assert claimed[0].available_at == 130
    other = await _row(sessionmaker_, second)
    assert other.state == "pending" and other.attempts == 0


async def test_fail_retries_then_exhausted(sessionmaker_):
    job_id = await _enqueue(sessionmaker_)
    async with sessionmaker_() as db:
        job = (await notify_queue.claim(db, "node-a", 100, 10, 30))[0]
        assert await notify_queue.fail(db, job, 100, max_attempts=3) is True
    row = await _row(sessionmaker_, job_id)
    assert row.state == "pending" and row.attempts == 1 and row.available_at == 115

    async with sessionmaker_() as db:
        assert await notify_queue.claim(db, "node-b", 114, 10, 30) == []
        job = (await notify_queue.claim(db, "node-b", 115, 10, 30))[0]
        assert job.attempts == 2 and job.claimed_generation == 2
        assert await notify_queue.fail(db, job, 115, max_attempts=3) is True
    row = await _row(sessionmaker_, job_id)
    assert row.state == "pending" and row.available_at == 145  # 15 * 2**(2-1)

    async with sessionmaker_() as db:
        job = (await notify_queue.claim(db, "node-a", 145, 10, 30))[0]
        assert job.attempts == 3
        assert await notify_queue.fail(db, job, 145, max_attempts=3) is True
    row = await _row(sessionmaker_, job_id)
    assert row.state == "failed"


async def test_complete(sessionmaker_):
    job_id = await _enqueue(sessionmaker_)
    async with sessionmaker_() as db:
        job = (await notify_queue.claim(db, "node-a", 100, 10, 30))[0]
        assert await notify_queue.complete(db, job, 110) is True
    row = await _row(sessionmaker_, job_id)
    assert row.state == "sent" and row.updated_at == 110
    async with sessionmaker_() as db:
        assert await notify_queue.claim(db, "node-b", 200, 10, 30) == []


async def test_expired_sending_taken_by_other_node(sessionmaker_):
    job_id = await _enqueue(sessionmaker_)
    async with sessionmaker_() as db:
        first = (await notify_queue.claim(db, "node-a", 100, 10, 30))[0]
    async with sessionmaker_() as db:
        assert await notify_queue.claim(db, "node-b", 129, 10, 30) == []
        second = (await notify_queue.claim(db, "node-b", 130, 10, 30))[0]
    assert second.id == job_id
    assert second.claimed_by == "node-b"
    assert second.claimed_generation == 2 and second.attempts == 2
    assert first.claimed_generation == 1


async def test_complete_with_stale_generation_does_not_apply(sessionmaker_):
    await _enqueue(sessionmaker_)
    async with sessionmaker_() as db:
        stale = (await notify_queue.claim(db, "node-a", 100, 10, 30))[0]
    async with sessionmaker_() as db:
        current = (await notify_queue.claim(db, "node-b", 130, 10, 30))[0]
    async with sessionmaker_() as db:
        assert await notify_queue.complete(db, stale, 131) is False
        assert await notify_queue.complete(db, current, 131) is True
    row = await _row(sessionmaker_, current.id)
    assert row.state == "sent" and row.claimed_generation == 2


async def test_purge_drops_old_terminal_jobs_only(sessionmaker_):
    now = 1_000_000
    old = now - 8 * 86400
    async with sessionmaker_() as db:
        db.add_all([
            NotificationJob(kind="manual", message="old-sent", state="sent", attempts=1,
                            available_at=old, created_at=old, updated_at=old),
            NotificationJob(kind="manual", message="old-failed", state="failed", attempts=5,
                            available_at=old, created_at=old, updated_at=old),
            NotificationJob(kind="manual", message="fresh-sent", state="sent", attempts=1,
                            available_at=now, created_at=now, updated_at=now - 86400),
            NotificationJob(kind="manual", message="old-pending", state="pending", attempts=0,
                            available_at=old, created_at=old, updated_at=old),
        ])
        await db.commit()
    async with sessionmaker_() as db:
        removed = await notify_queue.purge_finished(db, now, 7)
    assert removed == 2
    async with sessionmaker_() as db:
        left = {row.message for row in (await db.execute(select(NotificationJob))).scalars()}
    assert left == {"fresh-sent", "old-pending"}


@pytest_asyncio.fixture
async def file_sm(tmp_path):
    # Separate connections per session (a shared StaticPool connection would merge transactions).
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'q.db'}", connect_args={"timeout": 30})
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(eng, expire_on_commit=False)
    await eng.dispose()


async def _claim(sm, node):
    async with sm() as db:
        return await notify_queue.claim(db, node, 100, 10, 30)


async def test_concurrent_claim_single_winner(file_sm):
    await _enqueue(file_sm)
    results = await asyncio.gather(_claim(file_sm, "a"), _claim(file_sm, "b"))
    won = [job for batch in results for job in batch]
    assert len(won) == 1
    assert won[0].state == "sending"


async def test_partial_delivery_is_not_repeated(sessionmaker_, mk_user, monkeypatch):
    """One recipient accepted, one did not: the retry goes only to the one who missed it."""
    from app.services import notify_worker

    ok_id = await mk_user("ok@x.io")
    miss_id = await mk_user("miss@x.io")
    async with sessionmaker_() as db:
        job = NotificationJob(
            kind="motion", device_type="cams", device_id=1, message="person",
            payload={"object_type": "person", "user_ids": [ok_id, miss_id]},
            state="pending", attempts=0, available_at=1_000, created_at=1_000, updated_at=1_000,
        )
        db.add(job)
        await db.commit()
        job_id = job.id

    calls: list[int] = []
    failing = {"id": miss_id}

    async def fake_notify(user, message, photo_path=None, *, photo_bytes=None, photo_name="photo.jpg"):
        calls.append(user.id)
        return user.id != failing["id"]

    monkeypatch.setattr(notify_worker, "AsyncSessionLocal", sessionmaker_)
    monkeypatch.setattr(notify_worker.push, "notify_user", fake_notify)

    assert await notify_worker.deliver_batch(now=1_000, node_id="n1") == 1
    assert set(calls) == {ok_id, miss_id}
    row = await _row(sessionmaker_, job_id)
    assert row.state == "pending" and row.attempts == 1
    assert set(row.payload["delivered"]) == {ok_id}
    assert row.payload["user_ids"] == [ok_id, miss_id]

    calls.clear()
    failing["id"] = None
    assert await notify_worker.deliver_batch(now=row.available_at, node_id="n1") == 1
    assert calls == [miss_id]
    sent = await _row(sessionmaker_, job_id)
    assert sent.state == "sent"
