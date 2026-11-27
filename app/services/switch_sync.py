import asyncio
import logging
import time

from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.switch import Switch
from app.services import notify_queue, switch_client

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


def _switch_label(switch: Switch) -> str:
    return switch.name or str(switch.id)


async def _sync_one(db, switch: Switch) -> None:
    try:
        state = await switch_client.get_state(switch, db=db)
    except Exception:
        logger.exception("switch sync failed for switch_id=%s", switch.id)
        return

    if state == switch.state:
        return

    previous = switch.state
    switch.state = state
    switch.state_updated_at = int(time.time())

    if switch.notify == 1:
        from datetime import datetime
        now = datetime.fromtimestamp(time.time()).strftime("%d.%m.%Y %H:%M:%S")
        message = f"🔌 Устройство {_switch_label(switch)} изменило состояние: {'включено' if state == 1 else 'выключено'}\n\nВремя: {now}"
        await notify_queue.enqueue(
            db, kind="switch", device_type="switches", device_id=switch.id,
            message=message, payload={"filter": "notify_state_change"},
        )
    await db.commit()

    logger.info("switch_id=%s state changed %s -> %s via sync", switch.id, previous, state)


async def _sync_once() -> None:
    async with AsyncSessionLocal() as db:
        ids = (await db.execute(select(Switch.id).where(Switch.enabled == 1))).scalars().all()
    for switch_id in ids:
        # Read each device and its owner's network permission in a fresh
        # transaction, even when the preceding device reported no change.
        async with AsyncSessionLocal() as db:
            switch = await db.get(Switch, switch_id)
            if switch is None or not switch.enabled or not switch_client.supports_sync(switch):
                continue
            await _sync_one(db, switch)


async def _loop() -> None:
    while True:
        try:
            await _sync_once()
        except Exception:
            logger.exception("switch sync iteration failed")
        await asyncio.sleep(settings.SWITCH_SYNC_INTERVAL_SECONDS)


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
