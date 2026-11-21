"""Deferred ``sessions.last_seen`` refresh.

Authentication must not write to the database on the request path. ``touch`` only records
the latest observed activity per session in memory; a single background worker flushes the
batch every ``FLUSH_INTERVAL`` seconds. Every write is conditional (same token hash, session
still live, last_seen strictly increases), so it never resurrects a deleted/revoked session
and never moves ``last_seen`` backwards. Concurrent flushes from several server instances are
therefore safe. ``updated_at`` is intentionally not touched.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import update

from app.db import session as db_session
from app.models.session import Session

log = logging.getLogger(__name__)

IDLE_SECONDS = 30 * 24 * 3600
ACTIVITY_INTERVAL = 60
FLUSH_INTERVAL = 1.5
MAX_ENTRIES = 10000
STOP_TIMEOUT = 5.0

# session id -> (token_hash, timestamp): latest value waiting to be written.
_pending: dict[int, tuple[str, int]] = {}
# session id -> (token_hash, timestamp): last value scheduled/flushed (in-memory throttle).
_recent: dict[int, tuple[str, int]] = {}
_task: asyncio.Task | None = None
_flush_lock: asyncio.Lock | None = None


def touch(session_id: int, token_hash: str, now: int) -> None:
    """Record activity for a session. Synchronous, non-blocking, never raises."""
    try:
        seen = _recent.get(session_id)
        if seen is not None and seen[0] == token_hash and seen[1] > now - ACTIVITY_INTERVAL:
            return
        if session_id not in _pending and len(_pending) >= MAX_ENTRIES:
            return  # over the cap: ignore; the next request after the interval retries
        if len(_recent) >= MAX_ENTRIES * 2:
            _prune_recent(now)
        _pending[session_id] = (token_hash, now)
        _recent[session_id] = (token_hash, now)
    except Exception:  # pragma: no cover - defensive, touch must never break auth
        pass


def _prune_recent(now: int) -> None:
    for sid in [s for s, (_, ts) in _recent.items() if ts <= now - ACTIVITY_INTERVAL]:
        _recent.pop(sid, None)
    if len(_recent) >= MAX_ENTRIES * 2:
        _recent.clear()


def pending_count() -> int:
    return len(_pending)


def reset() -> None:
    """Drop all in-memory state (tests)."""
    global _task, _flush_lock
    _pending.clear()
    _recent.clear()
    if _task is not None and not _task.done():
        try:
            _task.cancel()
        except RuntimeError:  # event loop already closed
            pass
    _task = None
    _flush_lock = None


async def flush() -> None:
    """Write all pending entries in one short transaction. Raises on DB failure
    (entries are kept for the next attempt)."""
    global _flush_lock
    if _flush_lock is None:
        _flush_lock = asyncio.Lock()
    async with _flush_lock:
        if not _pending:
            return
        batch = dict(_pending)
        # Remove only what we took; newer values stored meanwhile replace the dict entries.
        for sid, val in batch.items():
            if _pending.get(sid) == val:
                _pending.pop(sid, None)
        try:
            async with db_session.AsyncSessionLocal() as db:
                for sid, (token_hash, ts) in batch.items():
                    await db.execute(update(Session).where(
                        Session.id == sid, Session.token_hash == token_hash,
                        Session.last_seen > ts - IDLE_SECONDS, Session.last_seen < ts,
                    ).values(last_seen=ts))
                await db.commit()
        except BaseException:
            for sid, val in batch.items():
                if len(_pending) < MAX_ENTRIES or sid in _pending:
                    cur = _pending.get(sid)
                    if cur is None or cur[1] < val[1]:
                        _pending[sid] = val
            raise


async def _worker() -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        try:
            await flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Session activity flush failed (%s); will retry", type(exc).__name__)


def start() -> None:
    """Start the background writer (idempotent)."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_worker())


async def stop(timeout: float = STOP_TIMEOUT) -> None:
    """Stop the worker and flush pending entries within ``timeout`` seconds (idempotent)."""
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    try:
        await asyncio.wait_for(flush(), timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Final session activity flush failed (%s)", type(exc).__name__)
