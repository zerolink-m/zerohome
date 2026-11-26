from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.cams import Cam
from app.services import notify_queue


logger = logging.getLogger(__name__)


_last_online: dict[int, bool] = {}
_task: asyncio.Task | None = None


async def _is_reachable(cam: Cam) -> bool:
    """A short TCP connect-and-close — no persistent connection is kept to the camera."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(cam.addr, cam.port),
            timeout=settings.HEALTHCHECK_TCP_TIMEOUT_SECONDS,
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


def _cam_label(cam: Cam) -> str:
    return cam.name or str(cam.id)


async def _check_once() -> None:
    async with AsyncSessionLocal() as db:
        # Check all enabled cameras (not just notify=1, as push_settings handles it now)
        cams = (
            await db.execute(
                select(Cam).where(Cam.enabled == 1, Cam.connection_resource_id.is_(None))
            )
        ).scalars().all()
        observed: dict[int, bool] = {}
        queued = False
        for cam in cams:
            reachable = await _is_reachable(cam)
            previous = _last_online.get(cam.id)
            observed[cam.id] = reachable

            if previous is None or previous == reachable:
                continue

            if reachable:
                message = f"✅ Камера {_cam_label(cam)} снова на связи\n\nВремя: {_format_time()}"
                filt = "notify_online"
            else:
                message = f"⚠️ Камера {_cam_label(cam)} не отвечает\n\nВремя: {_format_time()}"
                filt = "notify_offline"
            # Same transaction as this check's session; the worker delivers it.
            await notify_queue.enqueue(
                db, kind="health", device_type="cams", device_id=cam.id,
                message=message, payload={"filter": filt},
            )
            queued = True

        if queued:
            await db.commit()

    # Memory updates only after the jobs are durable, so a failed commit retries next cycle.
    _last_online.clear()
    _last_online.update(observed)


def _format_time() -> str:
    """Format current time as DD.MM.YYYY HH:MM:SS"""
    import time
    from datetime import datetime
    return datetime.fromtimestamp(time.time()).strftime("%d.%m.%Y %H:%M:%S")


async def _loop() -> None:
    while True:
        try:
            await _check_once()
        except Exception:
            logger.exception("healthcheck iteration failed")
        await asyncio.sleep(settings.HEALTHCHECK_INTERVAL_SECONDS)


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
    _last_online.clear()  # in-memory online/offline state is not valid after a handover
