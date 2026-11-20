"""Motion detection service using YOLOv8."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, Optional

import cv2
import numpy as np

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.cams import Cam
from app.models.motion_event import MotionEvent
from app.models.push_settings import PushSettings
from app.models.user import User
from app.services import license_plate_ocr, push
from app.services.object_tracker import ObjectTracker

logger = logging.getLogger("app.services.motion_detection")
from app.services.motion_detection.session import MotionDetectionSession, pool as process_pool


_active_sessions: Dict[int, 'MotionDetectionSession'] = {}
_STOP_ALL_TIMEOUT_SECONDS = 8.0


async def start_motion_detection(cam_id: int) -> bool:
    """Start motion detection for a camera."""
    # Central gate: startup, explicit API starts and future camera/settings hooks
    # all pass here. Do not touch the DB, model or ffmpeg while disabled.
    if not settings.MOTION_DETECTION_ENABLED:
        return False
    logger.debug(f"[Cam {cam_id}] start_motion_detection() called")

    existing = _active_sessions.get(cam_id)
    if existing is not None:
        task = existing.task
        alive = existing.is_running and task is not None and not task.done()
        if alive:
            logger.info(f"[Cam {cam_id}] Motion detection already running")
            return True
        # Retries exhausted: the session stays in the map with is_running False.
        # Starting it again is the only way the next call does not return True and do nothing.
        logger.info(f"[Cam {cam_id}] Motion detection session is not running, starting again")
        await existing.start()
        return bool(existing.is_running)

    try:
        async with AsyncSessionLocal() as db:
            # Get camera
            cam = await db.get(Cam, cam_id)
            if not cam:
                logger.warning(f"[Cam {cam_id}] Camera not found in database")
                return False

            if cam.enabled != 1:
                logger.warning(f"[Cam {cam_id}] Camera is disabled (enabled={cam.enabled})")
                return False

            logger.debug(f"[Cam {cam_id}] Camera found: {cam.name} at {cam.addr}:{cam.port}")

            # Get all users with motion detection enabled for this camera
            from sqlalchemy import select
            stmt = select(PushSettings).where(
                PushSettings.device_type == "cams",
                PushSettings.device_id == cam_id,
                PushSettings.motion_detection_enabled == 1,
            )
            result = await db.execute(stmt)
            user_settings = result.scalars().all()

            if not user_settings:
                logger.info(f"[Cam {cam_id}] No users have motion_detection_enabled=1 for this camera")
                return False

            logger.debug(f"[Cam {cam_id}] Found {len(user_settings)} user(s) with motion detection enabled")
            for ps in user_settings:
                logger.debug(f"[Cam {cam_id}]   - user_id={ps.user_id}: "
                           f"person={ps.motion_detect_person}, "
                           f"dog={ps.motion_detect_dog}, "
                           f"cat={ps.motion_detect_cat}, "
                           f"car={ps.motion_detect_car}, "
                           f"min_conf={ps.motion_min_confidence}, "
                           f"min_size={ps.motion_min_object_size}, "
                           f"cooldown={ps.motion_cooldown_seconds}s")

            # Create session
            logger.debug(f"[Cam {cam_id}] Creating MotionDetectionSession...")
            session = MotionDetectionSession(cam, user_settings)
            _active_sessions[cam_id] = session

            # Start
            logger.debug(f"[Cam {cam_id}] Starting session...")
            await session.start()
            logger.info(f"[Cam {cam_id}] Motion detection started successfully")
            return True

    except Exception as exc:
        logger.error(f"[Cam {cam_id}] Failed to start motion detection: {exc}", exc_info=True)
        return False


async def stop_motion_detection(cam_id: int) -> bool:
    """Stop motion detection for a camera."""
    session = _active_sessions.pop(cam_id, None)
    if session:
        await session.stop()
        return True
    return False


def is_motion_detection_active(cam_id: int) -> bool:
    """Check if motion detection is active for a camera."""
    return cam_id in _active_sessions


async def startup() -> None:
    """Reaps detection ffmpegs left by a previous run (registry + start time + env tag,
    never by name). No-op unless INGEST_ENABLED. Call once at app startup, before any session."""
    await process_pool.startup()


async def stop_all_sessions(timeout: float = _STOP_ALL_TIMEOUT_SECONDS):
    """Stop all motion detection sessions (within `timeout` overall, then SIGKILL of
    whatever ffmpeg is left)."""
    tasks = [asyncio.create_task(stop_motion_detection(cam_id)) for cam_id in list(_active_sessions.keys())]
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=1.0)
    await process_pool.stop_all(timeout=2.0)
    process_pool.force_kill_all()
