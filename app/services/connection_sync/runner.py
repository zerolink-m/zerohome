from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.connection import Connection
from app.models.invitation import Invitation
from app.models.sharing import Sharing
from app.models.switch import Switch
from app.services import tuya_client_pool
from app.services.tuya import Function, Specifications, TuyaError

logger = logging.getLogger("app.services.connection_sync")

from app.services.connection_sync.tuya_sync import _sync_tuya_connection

_task: asyncio.Task | None = None


async def _sync_one(db, conn: Connection) -> None:
    """Dispatch on provider; record success/failure on the Connection row."""
    if conn.provider == "ufanet-smart-home":
        from app.services.connection_sync.ufanet_sync import sync_ufanet

        connection_id = conn.id
        try:
            await sync_ufanet(db, connection_id)
        except Exception as exc:
            await db.rollback()
            current = await db.get(Connection, connection_id)
            if current is not None:
                current.status = "unavailable"
                current.last_checked_at = int(time.time())
                current.last_sync_error = f"Ufanet: {type(exc).__name__}"
                current.updated_at = current.last_checked_at
                await db.commit()
            logger.warning("Ufanet sync failed for connection_id=%s: %s", connection_id, type(exc).__name__)
        return
    now = int(time.time())
    try:
        if conn.provider == "tuya_cloud":
            await _sync_tuya_connection(db, conn)
        else:
            # Unknown provider — shouldn't happen given the enum, but be loud
            # if it does rather than silently marking success.
            raise RuntimeError(f"неизвестный provider: {conn.provider!r}")
    except TuyaError as exc:
        # Rollback any partial writes from _sync_tuya_connection so the
        # last_sync_error update goes in on a clean session.
        await db.rollback()
        conn.last_sync_error = str(exc)[:1000]
        conn.updated_at = now
        await db.commit()
        logger.warning(
            "connection sync failed for connection_id=%s: %s", conn.id, exc
        )
        return
    except ValueError as exc:
        # Misconfiguration (e.g. missing tuya_uid) — user-actionable, record
        # the message verbatim so the client can show what to fix.
        await db.rollback()
        conn.last_sync_error = str(exc)[:1000]
        conn.updated_at = now
        await db.commit()
        logger.warning(
            "connection sync misconfigured for connection_id=%s: %s", conn.id, exc
        )
        return
    except Exception as exc:
        await db.rollback()
        conn.last_sync_error = f"внутренняя ошибка: {exc.__class__.__name__}"[:1000]
        conn.updated_at = now
        await db.commit()
        logger.exception("connection sync crashed for connection_id=%s", conn.id)
        return

    conn.last_synced_at = now
    conn.last_sync_error = None
    conn.updated_at = now
    await db.commit()
    logger.info("connection sync ok for connection_id=%s", conn.id)


async def _sync_once() -> None:
    async with AsyncSessionLocal() as db:
        conns = (
            await db.execute(select(Connection).where(Connection.enabled == 1))
        ).scalars().all()
        for conn in conns:
            await _sync_one(db, conn)


async def _loop() -> None:
    while True:
        try:
            await _sync_once()
        except Exception:
            logger.exception("connection sync iteration failed")
        await asyncio.sleep(settings.CONNECTION_SYNC_INTERVAL_SECONDS)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    """Stops the loop only (idempotent; safe when it was never started)."""
    global _task
    task, _task = _task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("%s loop raised during stop", __name__)


async def sync_connection_now(connection_id: int) -> None:
    """Manually trigger a sync for one connection (called from the router on
    connection create/update, so the client sees devices without waiting for
    the next scheduled tick)."""
    async with AsyncSessionLocal() as db:
        conn = await db.get(Connection, connection_id)
        if conn is None or conn.enabled != 1:
            return
        await _sync_one(db, conn)
