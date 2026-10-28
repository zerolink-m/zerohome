from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.cams import Cam
from app.services import push


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
                select(Cam).where(Cam.enabled == 1)
            )
        ).scalars().all()
        for cam in cams:
            reachable = await _is_reachable(cam)
            previous = _last_online.get(cam.id)
            _last_online[cam.id] = reachable

            if previous is None or previous == reachable:
                continue

            if reachable:
                message = f"✅ Камера {_cam_label(cam)} снова на связи\n\nВремя: {_format_time()}"
                # Notify users with notify_online=1
                await push._notify_recipients_filtered(
                    db, "cams", cam.id, cam.ownuser, message,
                    filter_func=lambda ps: ps.notify_online == 1
                )
            else:
                message = f"⚠️ Камера {_cam_label(cam)} не отвечает\n\nВремя: {_format_time()}"
                # Notify users with notify_offline=1
                await push._notify_recipients_filtered(
                    db, "cams", cam.id, cam.ownuser, message,
                    filter_func=lambda ps: ps.notify_offline == 1
                )

        still_tracked = {cam.id for cam in cams}
        for stale_id in list(_last_online.keys()):
            if stale_id not in still_tracked:
                _last_online.pop(stale_id, None)


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
