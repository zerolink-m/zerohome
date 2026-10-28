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

            # Delete photos from filesystem
            deleted_files = 0
            for event in old_events:
                photo_path = os.path.join(
                    settings.motion_files_path,
                    str(event.cam_id),
                    event.photo_filename
                )

                if os.path.exists(photo_path):
                    try:
                        os.remove(photo_path)
                        deleted_files += 1
                    except Exception as exc:
                        logger.warning(f"Failed to delete {photo_path}: {exc}")

            # Delete from database
            stmt_delete = delete(MotionEvent).where(MotionEvent.detected_at < seven_days_ago)
            await db.execute(stmt_delete)
            await db.commit()

            logger.info(f"Cleaned up {len(old_events)} events and {deleted_files} photos")

            # Clean up empty directories
            motion_files_dir = settings.motion_files_path
            if os.path.exists(motion_files_dir):
                for cam_dir in os.listdir(motion_files_dir):
                    cam_path = os.path.join(motion_files_dir, cam_dir)
                    if os.path.isdir(cam_path) and not os.listdir(cam_path):
                        try:
                            os.rmdir(cam_path)
                            logger.debug(f"Removed empty directory: {cam_path}")
                        except Exception as exc:
                            logger.warning(f"Failed to remove directory {cam_path}: {exc}")

    except Exception as exc:
        logger.exception(f"Motion cleanup failed: {exc}")


async def _loop() -> None:
    """Main cleanup loop."""
    while True:
        await _cleanup_once()
        await asyncio.sleep(_cleanup_interval_seconds)


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
