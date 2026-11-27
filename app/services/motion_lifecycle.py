"""Lifecycle service for motion detection - auto-start on cameras with enabled motion."""
from __future__ import annotations

import logging

from sqlalchemy.orm import aliased

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.cams import Cam
from app.models.user import User
from app.services import motion_detection
from app.services.cluster.identity import worker_enabled
from app.services.motion_detection.manager import eligible_preferences

logger = logging.getLogger(__name__)


async def select_cams() -> list[tuple[int, str]]:
    """(cam_id, name) of cameras where motion detection should run: enabled camera,
    a user turned it on in push_settings, and the camera owner is allowed to use it."""
    owner = aliased(User)
    stmt = (
        eligible_preferences()
        .with_only_columns(Cam.id, Cam.name)
        .join(owner, owner.id == Cam.ownuser)
        .where(Cam.enabled == 1, Cam.connection_resource_id.is_(None), owner.motion_detection_allowed == 1)
        .distinct()
    )
    async with AsyncSessionLocal() as db:
        return [(cam_id, name) for cam_id, name in (await db.execute(stmt)).all()]


async def on_preferences_changed(cam_id: int) -> None:
    if settings.CLUSTER_ENABLED:
        from app.services.cluster.runner import request_reconcile
        request_reconcile()
    elif worker_enabled(settings, "motion"):
        # The running session refreshes its settings and exits itself when none remain.
        await motion_detection.start_motion_detection(cam_id)


async def on_cam_saved(cam: Cam, fields: set[str]) -> None:
    if settings.CLUSTER_ENABLED:
        return  # Camera changes already request cluster reconciliation.
    if fields & {"enabled", "protocol", "address", "port", "path", "username", "password", "name"}:
        await motion_detection.stop_motion_detection(cam.id)
        if cam.enabled == 1:
            await on_preferences_changed(cam.id)


async def on_cam_deleted(cam_id: int) -> None:
    if not settings.CLUSTER_ENABLED:
        await motion_detection.stop_motion_detection(cam_id)


async def start_all_enabled() -> None:
    """Start motion detection on all cameras where users have it enabled."""
    if not worker_enabled(settings, "motion"):
        return
    try:
        cams = await select_cams()
        if not cams:
            logger.info("No cameras with motion detection enabled")
            return

        started = 0
        for cam_id, name in cams:
            success = await motion_detection.start_motion_detection(cam_id)
            if success:
                started += 1
                logger.info(f"Started motion detection for cam {cam_id} ({name})")

        logger.info(f"Auto-started motion detection on {started} cameras")

    except Exception as exc:
        logger.error(f"Failed to auto-start motion detection: {exc}")


async def stop_all() -> None:
    """Stop all motion detection sessions."""
    await motion_detection.stop_all_sessions()
    logger.info("Stopped all motion detection sessions")
