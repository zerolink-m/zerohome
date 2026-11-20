"""Cleanup service for old motion events (7 days)."""
from __future__ import annotations

import asyncio
import logging
import os
import time

from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.motion_event import MotionEvent
from app.services import storage as storage_layer

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_cleanup_interval_seconds = 3600  # Run every hour


async def _cleanup_once() -> None:
    """Clean up motion events older than 7 days."""
    try:
        seven_days_ago = int(time.time()) - (7 * 24 * 60 * 60)

        async with AsyncSessionLocal() as db:
            # Get old events
            stmt = select(MotionEvent).where(MotionEvent.detected_at < seven_days_ago)
            result = await db.execute(stmt)
            old_events = result.scalars().all()

            if not old_events:
                logger.debug("No old motion events to clean up")
                return

            logger.info(f"Cleaning up {len(old_events)} motion events older than 7 days")

            deleted_files = 0
            storage = storage_layer.get_storage()
            for event in old_events:
                if event.photo_key:
                    try:
                        if await storage.exists(event.photo_key):
                            await storage.delete(event.photo_key)
                            deleted_files += 1
                        else:
                            await storage.delete(event.photo_key)
                    except Exception as exc:
                        logger.warning(
                            "failed to delete motion photo for event %s: %s",
                            event.id, type(exc).__name__,
                        )
                elif event.photo_filename:
                    photo_path = storage_layer.legacy_motion_path(event.cam_id, event.photo_filename)
                    if os.path.isfile(photo_path):
                        try:
                            os.remove(photo_path)
                            deleted_files += 1
                        except Exception as exc:
                            logger.warning(
                                "failed to delete legacy motion photo for event %s: %s",
                                event.id, type(exc).__name__,
                            )

            # Delete from database
            stmt_delete = delete(MotionEvent).where(MotionEvent.detected_at < seven_days_ago)
            await db.execute(stmt_delete)
            await db.commit()

            logger.info(f"Cleaned up {len(old_events)} events and {deleted_files} photos")
            _prune_empty_dirs(settings.motion_files_path)

    except Exception as exc:
        logger.exception(f"Motion cleanup failed: {exc}")


async def _loop() -> None:
    """Main cleanup loop."""
    while True:
        await _cleanup_once()
        await asyncio.sleep(_cleanup_interval_seconds)


def _prune_empty_dirs(root: str) -> None:
    """Drop empty directories left under the local photo root. Never removes the root itself."""
    if not os.path.isdir(root):
        return
    root_abs = os.path.abspath(root)
    for dirpath, _dirnames, _filenames in os.walk(root_abs, topdown=False):
        if os.path.abspath(dirpath) == root_abs:
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError as exc:
            logger.warning("failed to remove directory %s: %s", dirpath, type(exc).__name__)


def start() -> None:
    """Start cleanup task."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
        logger.info("Motion detection cleanup task started")


async def stop() -> None:
    """Stop cleanup task."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        logger.info("Motion detection cleanup task stopped")
