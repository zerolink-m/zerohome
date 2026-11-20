"""DB queue of outbound notifications.

enqueue() only inserts: the caller commits, so the job lands in the same transaction as the
event that caused it. claim/complete/fail/defer are conditional updates on
(id, claimed_generation). claimed_generation is attempts after a successful claim (1, 2, ...).
A node that lost the job — lease expired, another node claimed it — no longer matches, so it
cannot complete or fail the row. Two claims race on ``state in (pending, sending) AND
available_at <= now AND attempts = <what we read>``; rowcount == 1 wins.
"""
from __future__ import annotations

import time

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import KINDS, NotificationJob

_NO_MATCH = {"synchronize_session": False}


def retry_delay(attempts: int) -> int:
    """Backoff after a failed send. ``attempts`` is the value stored by claim (1-based)."""
    power = min(10, max(0, attempts - 1))
    return min(300, 15 * (2 ** power))


async def enqueue(
    db: AsyncSession,
    *,
    kind: str,
    message: str,
    now: int | None = None,
    device_type: str | None = None,
    device_id: int | None = None,
    photo_key: str | None = None,
    payload: dict | None = None,
    available_at: int | None = None,
) -> NotificationJob:
    """Insert a pending job. Does not commit."""
    if kind not in KINDS:
        raise ValueError(f"unknown notification kind {kind!r}")
    ts = int(time.time()) if now is None else int(now)
    job = NotificationJob(
        kind=kind,
        device_type=device_type,
        device_id=device_id,
        message=message,
        photo_key=photo_key,
        payload=payload,
        state="pending",
        attempts=0,
        available_at=ts if available_at is None else int(available_at),
        created_at=ts,
        updated_at=ts,
    )
    db.add(job)
    await db.flush()
    return job


async def claim(
    db: AsyncSession,
    node_id: str,
    now: int,
    limit: int,
    lease_seconds: int,
) -> list[NotificationJob]:
    """Take up to ``limit`` jobs that are due: pending, or sending whose lease has expired."""
    if limit < 1:
        return []
    rows = (
        await db.execute(
            select(NotificationJob.id, NotificationJob.attempts)
            .where(
                NotificationJob.available_at <= now,
                NotificationJob.state.in_(("pending", "sending")),
            )
            .order_by(NotificationJob.available_at, NotificationJob.id)
            .limit(limit)
        )
    ).all()
    await db.rollback()  # release the read so another node can update the same rows
    won: list[int] = []
    for job_id, attempts in rows:
        new_attempts = int(attempts) + 1
        res = await db.execute(
            update(NotificationJob)
            .where(
                NotificationJob.id == job_id,
                NotificationJob.attempts == attempts,
                NotificationJob.available_at <= now,
                NotificationJob.state.in_(("pending", "sending")),
            )
            .values(
                state="sending",
                claimed_by=node_id,
                claimed_generation=new_attempts,
                attempts=new_attempts,
                available_at=now + lease_seconds,
                updated_at=now,
            ),
            execution_options=_NO_MATCH,
        )
        if res.rowcount == 1:
            won.append(job_id)
        await db.commit()
    if not won:
        return []
    loaded = (
        await db.execute(select(NotificationJob).where(NotificationJob.id.in_(won)))
    ).scalars().all()
    # Detach before the caller closes the session: close() rollbacks and would expire them.
    for row in loaded:
        db.expunge(row)
    by_id = {row.id: row for row in loaded}
    return [by_id[job_id] for job_id in won if job_id in by_id]


def _owned(job: NotificationJob):
    return (
        NotificationJob.id == job.id,
        NotificationJob.state == "sending",
        NotificationJob.claimed_generation == job.claimed_generation,
    )


async def complete(db: AsyncSession, job: NotificationJob, now: int) -> bool:
    """Mark the job sent. False when this generation no longer owns it."""
    res = await db.execute(
        update(NotificationJob).where(*_owned(job)).values(state="sent", updated_at=now),
        execution_options=_NO_MATCH,
    )
    if res.rowcount == 1:
        await db.commit()
        return True
    await db.rollback()
    return False


async def fail(db: AsyncSession, job: NotificationJob, now: int, max_attempts: int) -> bool:
    """Retry with backoff while attempts < max_attempts, otherwise mark failed.

    ``attempts`` was already incremented by claim, so the first failure is attempt 1.
    False when this generation no longer owns the job.
    """
    if int(job.attempts) < max_attempts:
        values = {
            "state": "pending",
            "available_at": now + retry_delay(int(job.attempts)),
            "updated_at": now,
        }
    else:
        values = {"state": "failed", "updated_at": now}
    res = await db.execute(
        update(NotificationJob).where(*_owned(job)).values(**values),
        execution_options=_NO_MATCH,
    )
    if res.rowcount == 1:
        await db.commit()
        return True
    await db.rollback()
    return False


async def defer(db: AsyncSession, job: NotificationJob, now: int, delay: int) -> bool:
    """Put the job back without counting the claim as a failed send (photo still uploading).

    attempts is undone and the generation is cleared, so the claimer cannot complete it later.
    """
    res = await db.execute(
        update(NotificationJob)
        .where(*_owned(job))
        .values(
            state="pending",
            available_at=now + delay,
            attempts=max(0, int(job.attempts) - 1),
            claimed_by=None,
            claimed_generation=None,
            updated_at=now,
        ),
        execution_options=_NO_MATCH,
    )
    if res.rowcount == 1:
        await db.commit()
        return True
    await db.rollback()
    return False


async def purge_finished(db: AsyncSession, now: int, retention_days: int) -> int:
    """Delete sent/failed jobs older than ``retention_days``. Pending and sending stay."""
    cutoff = now - int(retention_days) * 86400
    res = await db.execute(
        delete(NotificationJob).where(
            NotificationJob.state.in_(("sent", "failed")),
            NotificationJob.updated_at < cutoff,
        ),
        execution_options=_NO_MATCH,
    )
    await db.commit()
    return int(res.rowcount or 0)
