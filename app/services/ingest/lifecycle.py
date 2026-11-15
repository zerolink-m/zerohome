"""Application-level wiring: start/stop of ingest and camera CRUD hooks.

Everything is a no-op unless INGEST_ENABLED, and nothing here raises: an ingest
problem must never break the app or an API response."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.core.config import settings
from app.db import session as db_session
from app.models.cams import Cam
from app.services.ingest.supervisor import IngestSupervisor, IngestTarget
from app.services.rtsp_url import build_rtsp_url

logger = logging.getLogger(__name__)

# PATCH fields that change how ingest connects (or whether it runs)
RELEVANT_FIELDS = frozenset({"enabled", "protocol", "address", "port", "path", "username", "password"})
_HOOK_TIMEOUT_SECONDS = 5.0

_supervisor: IngestSupervisor | None = None


def get_supervisor() -> IngestSupervisor:
    global _supervisor
    if _supervisor is None:
        _supervisor = IngestSupervisor()
    return _supervisor


def peek_supervisor() -> IngestSupervisor | None:
    """The singleton if it exists; never creates one."""
    return _supervisor


def reset_supervisor() -> None:
    """Forgets the singleton (tests)."""
    global _supervisor
    _supervisor = None


def _target_for(cam: Cam) -> IngestTarget | None:
    if cam.enabled != 1 or not cam.addr:
        return None
    return IngestTarget(cam_id=cam.id, url=build_rtsp_url(cam), name=cam.name or "")


async def prepare() -> None:
    """Reaps ingest leftovers of the previous run. Never raises."""
    if not settings.INGEST_ENABLED:
        return
    try:
        report = await get_supervisor().startup()
        logger.info("ingest: orphan sweep: %s", report)
    except Exception:
        logger.exception("ingest: orphan sweep failed")


async def select_targets() -> dict[int, IngestTarget]:
    """Cameras ingest should run on right now (enabled=1 with an address), by cam id."""
    async with db_session.AsyncSessionLocal() as db:
        cams = (await db.execute(select(Cam).where(Cam.enabled == 1))).scalars().all()
        targets = [t for t in (_target_for(c) for c in cams) if t is not None]
    return {t.cam_id: t for t in targets}


async def start_all_enabled() -> None:
    """Reaps leftovers of the previous run, then starts ingest for every enabled camera."""
    if not settings.INGEST_ENABLED:
        return
    try:
        supervisor = get_supervisor()
        report = await supervisor.startup()
        logger.info("ingest: orphan sweep: %s", report)
        targets = list((await select_targets()).values())
        for target in targets:
            await supervisor.start(target)
        logger.info("ingest: started on %d cameras", len(targets))
    except Exception:
        logger.exception("ingest: failed to start")


async def stop_all() -> None:
    if not settings.INGEST_ENABLED or _supervisor is None:
        return
    try:
        timeout = _supervisor.config.stop_timeout_seconds
        # hard outer limit: stop_all force-kills on its own, this is the last guard
        await asyncio.wait_for(_supervisor.stop_all(timeout), timeout=timeout + 5.0)
    except Exception:
        logger.exception("ingest: failed to stop cleanly")
    else:
        logger.info("ingest: stopped")


async def _guarded(coro, what: str) -> None:
    try:
        await asyncio.wait_for(coro, timeout=_HOOK_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("ingest hook failed: %s", what)


def _request_cluster_reconcile() -> None:
    from app.services.cluster import runner
    runner.request_reconcile()


async def on_cam_saved(cam: Cam, fields: set[str] | None = None) -> None:
    """Camera created or PATCHed. `fields` = names the request touched (None = all).
    enabled=0 stops; enabled=1 starts/updates (unchanged URL is a no-op).
    CLUSTER_ENABLED: this node does not start anything itself (the camera may belong to
    another node); it only asks the runner for an early reconcile."""
    if settings.CLUSTER_ENABLED:
        if fields is None or (set(fields) & RELEVANT_FIELDS):
            _request_cluster_reconcile()
        return
    if not settings.INGEST_ENABLED:
        return
    if fields is not None and not (set(fields) & RELEVANT_FIELDS):
        return

    async def _apply() -> None:
        await get_supervisor().apply(cam.id, _target_for(cam))

    await _guarded(_apply(), f"cam {cam.id} saved")


async def on_cam_deleted(cam_id: int) -> None:
    if settings.CLUSTER_ENABLED:
        _request_cluster_reconcile()  # the owner notices the camera is gone and stops
        return
    if not settings.INGEST_ENABLED:
        return
    await _guarded(get_supervisor().stop(cam_id), f"cam {cam_id} deleted")
