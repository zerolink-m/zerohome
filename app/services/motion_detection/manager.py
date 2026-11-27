"""Motion detection service using YOLOv8."""
from __future__ import annotations

import asyncio
import logging
from weakref import WeakValueDictionary

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.cams import Cam
from app.models.invitation import Invitation
from app.models.push_settings import PushSettings
from app.models.user import User
from app.services.cluster.identity import worker_enabled
from app.services.motion_detection.session import MotionDetectionSession, pool as process_pool

logger = logging.getLogger("app.services.motion_detection")

_active_sessions: dict[int, MotionDetectionSession] = {}
_camera_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
_STOP_ALL_TIMEOUT_SECONDS = 8.0


def _lock(cam_id: int) -> asyncio.Lock:
    return _camera_locks.setdefault(cam_id, asyncio.Lock())


def eligible_preferences():
    """Motion preferences of users who currently have access to a camera."""
    invitation = select(Invitation.id).where(
        Invitation.device_type == "cams",
        Invitation.device_id == Cam.id,
        Invitation.status == "accepted",
        func.lower(Invitation.email) == func.lower(User.email),
    ).correlate(Cam, User).exists()
    return (
        select(PushSettings)
        .select_from(Cam)
        .join(PushSettings, PushSettings.device_id == Cam.id)
        .join(User, User.id == PushSettings.user_id)
        .where(
            PushSettings.device_type == "cams",
            PushSettings.motion_detection_enabled == 1,
            or_(User.id == Cam.ownuser, User.role == "admin", invitation),
        )
    )


async def _preferences(db: AsyncSession, cam_id: int) -> tuple[Cam | None, list[PushSettings]]:
    cam = await db.get(Cam, cam_id)
    if cam is None or cam.enabled != 1 or cam.connection_resource_id is not None:
        return None, []
    owner = await db.get(User, cam.ownuser)
    if owner is None or owner.motion_detection_allowed != 1:
        return None, []
    return cam, list((await db.scalars(eligible_preferences().where(Cam.id == cam_id))).all())


async def _refresh_preferences(cam_id: int) -> list[PushSettings]:
    async with AsyncSessionLocal() as db:
        _, preferences = await _preferences(db, cam_id)
        return preferences


async def start_motion_detection(cam_id: int) -> bool:
    """Start motion detection for a camera, serializing starts and stops."""
    if not worker_enabled(settings, "motion"):
        return False

    async with _lock(cam_id):
        if is_motion_detection_active(cam_id):
            return True
        try:
            async with AsyncSessionLocal() as db:
                cam, preferences = await _preferences(db, cam_id)
            if cam is None or not preferences:
                return False
            previous = _active_sessions.get(cam_id)
            if previous is not None:
                await previous.stop()
            session = MotionDetectionSession(cam, preferences)
            session.load_preferences = _refresh_preferences
            await session.start()
            _active_sessions[cam_id] = session
            return True
        except Exception:
            logger.exception("[Cam %s] Failed to start motion detection", cam_id)
            return False


async def stop_motion_detection(cam_id: int) -> bool:
    """Stop motion detection for a camera."""
    async with _lock(cam_id):
        session = _active_sessions.get(cam_id)
        if session is None:
            return False
        await session.stop()
        _active_sessions.pop(cam_id, None)
        return True


def is_motion_detection_active(cam_id: int) -> bool:
    session = _active_sessions.get(cam_id)
    return bool(session and session.is_running and session.task is not None and not session.task.done())


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
