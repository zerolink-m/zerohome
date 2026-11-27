from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.connection import Connection
from app.services.tuya import TuyaError

logger = logging.getLogger("app.services.connection_sync")

from app.services.connection_sync.tuya_sync import _sync_tuya_connection

_task: asyncio.Task | None = None


async def _sync_one(db, conn: Connection) -> None:
    """Dispatch by provider and record errors only for the revision checked."""
    connection_id, provider, revision = conn.id, conn.provider, conn.sync_revision
    try:
        if provider == "ufanet-smart-home":
            from app.services.connection_sync.ufanet_sync import sync_ufanet

            await sync_ufanet(db, connection_id)
        elif provider == "tuya_cloud":
            await _sync_tuya_connection(db, conn)
        else:
            raise RuntimeError(f"неизвестный provider: {provider!r}")
    except Exception as exc:
        await db.rollback()
        current = (await db.execute(
            select(Connection).where(Connection.id == connection_id)
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if (current is None or not current.enabled or current.provider != provider
                or current.sync_revision != revision):
            await db.rollback()
            return
        now = int(time.time())
        current.status = "unavailable"
        current.last_checked_at = now
        current.updated_at = now
        if provider == "tuya_cloud" and isinstance(exc, TuyaError):
            current.last_sync_error = f"Tuya: {type(exc).__name__} ({exc.code})"
        elif provider == "tuya_cloud" and type(exc) is ValueError and not (current.meta or {}).get("tuya_uid"):
            current.last_sync_error = "meta.tuya_uid не задан — синхронизация невозможна до его установки"
        else:
            current.last_sync_error = f"{provider}: {type(exc).__name__}"
        await db.commit()
        logger.warning("%s sync failed for connection_id=%s: %s", provider, connection_id, type(exc).__name__)


async def _sync_once() -> None:
    async with AsyncSessionLocal() as db:
        ids = (
            await db.execute(select(Connection.id).where(Connection.enabled == 1))
        ).scalars().all()
    for connection_id in ids:
        try:
            # One identity map per connection: rollback cannot expire the
            # rest of the connection list.
            await sync_connection_now(connection_id)
        except Exception:
            logger.exception("connection sync crashed for connection_id=%s", connection_id)


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
