import asyncio
import logging
import time

from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.switch import Switch
from app.services import push, switch_client

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


def _switch_label(switch: Switch) -> str:
    return switch.name or str(switch.id)


async def _sync_one(db, switch: Switch) -> None:
    try:
        state = await switch_client.get_state(switch)
    except Exception:
        logger.exception("switch sync failed for switch_id=%s", switch.id)
        return

    if state == switch.state:
        return

    previous = switch.state
    switch.state = state
    switch.state_updated_at = int(time.time())
    await db.commit()

    # Notify users with notify_state_change=1
    from datetime import datetime
    now = datetime.fromtimestamp(time.time()).strftime("%d.%m.%Y %H:%M:%S")
    message = f"🔌 Устройство {_switch_label(switch)} изменило состояние: {'включено' if state == 1 else 'выключено'}\n\nВремя: {now}"
    await push._notify_recipients_filtered(
        db, "switches", switch.id, switch.ownuser, message,
        filter_func=lambda ps: ps.notify_state_change == 1
    )

    logger.info("switch_id=%s state changed %s -> %s via sync", switch.id, previous, state)


async def _sync_once() -> None:
    async with AsyncSessionLocal() as db:
        switches = (await db.execute(select(Switch).where(Switch.enabled == 1))).scalars().all()
        for switch in switches:
            if not switch_client.supports_sync(switch):
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
