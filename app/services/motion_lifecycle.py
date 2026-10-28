"""Lifecycle service for motion detection - auto-start on cameras with enabled motion."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.cams import Cam
from app.models.push_settings import PushSettings
from app.models.user import User
from app.services import motion_detection

logger = logging.getLogger(__name__)


async def start_all_enabled() -> None:
    """Start motion detection on all cameras where users have it enabled."""
    try:
        async with AsyncSessionLocal() as db:
            # Get all cameras with motion detection enabled in push_settings
            stmt = select(PushSettings.device_id).where(
                PushSettings.device_type == "cams",
                PushSettings.motion_detection_enabled == 1,
            ).distinct()

            result = await db.execute(stmt)
            cam_ids = [row[0] for row in result.fetchall()]

            if not cam_ids:
                logger.info("No cameras with motion detection enabled")
                return

            # Check if users have motion_detection_allowed=1
            stmt_cams = select(Cam).where(
                Cam.id.in_(cam_ids),
                Cam.enabled == 1,
            )
            result_cams = await db.execute(stmt_cams)
            cameras = result_cams.scalars().all()

            started = 0
            for cam in cameras:
                # Check if owner has motion_detection_allowed
                owner = await db.get(User, cam.ownuser)
                if owner and owner.motion_detection_allowed == 1:
                    success = await motion_detection.start_motion_detection(cam.id)
                    if success:
                        started += 1
                        logger.info(f"Started motion detection for cam {cam.id} ({cam.name})")
                else:
                    logger.debug(f"Cam {cam.id}: owner doesn't have motion_detection_allowed")

            logger.info(f"Auto-started motion detection on {started} cameras")

    except Exception as exc:
        logger.error(f"Failed to auto-start motion detection: {exc}")


async def stop_all() -> None:
    """Stop all motion detection sessions."""
    await motion_detection.stop_all_sessions()
    logger.info("Stopped all motion detection sessions")
