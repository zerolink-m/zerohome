from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.errors import (
    AppError,
    app_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_error_handler,
)
from app.core.logging import setup_logging
from app.core.middleware import CursedMiddleware
from app.core.rate_limit import start_cleanup_task
from app.routers import auth, cams, config, connections, invitations, motion, push_settings, service, sessions, shares, stats, switches, users, webrtc
from app.services import ingest
from app.services import (
    connection_sync,
    healthcheck,
    hls_manager,
    motion_cleanup,
    motion_detection,
    motion_lifecycle,
    snapshot,
    stream_manager,
    switch_sync,
    tuya_client_pool,
    tuya_events,
    webrtc_manager,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup (same order as before) ---
    start_cleanup_task()
    if settings.SESSION_CLEANUP_ENABLED:
        auth.start_session_cleanup()
    if settings.HEALTHCHECK_ENABLED:
        healthcheck.start()
    if settings.SWITCH_SYNC_ENABLED:
        switch_sync.start()
    if settings.CONNECTION_SYNC_ENABLED:
        connection_sync.start()
    # Independent of polling: disabling one does not disable the other.
    if settings.TUYA_EVENTS_ENABLED:
        tuya_events.start()
    if settings.HLS_ENABLED:
        hls_manager.start_cleanup_task()
    # Motion detection cleanup task (removes events older than 7 days)
    if settings.MOTION_CLEANUP_ENABLED:
        motion_cleanup.start()
    # Persistent ingest ffmpeg per enabled camera (no-op unless INGEST_ENABLED).
    # Before motion detection: its sessions read the hub.
    await ingest.start_all_enabled()
    # Reap transcoders orphaned by a previous run (no-op unless INGEST_ENABLED)
    await stream_manager.startup()
    # Reap HLS ffmpegs orphaned by a previous run
    await hls_manager.startup()
    # Same for WebRTC decoders (separate registry; no-op unless INGEST_ENABLED)
    await webrtc_manager.startup()
    # Same for hub snapshots and motion detection decoders (separate registries)
    await snapshot.startup()
    await motion_detection.startup()
    # Auto-start motion detection on cameras where users have it enabled
    await motion_lifecycle.start_all_enabled()

    yield

    # --- shutdown (same order as before) ---
    # Everything here is bounded (ingest: INGEST_STOP_TIMEOUT_SECONDS, then SIGKILL) — must not hang.
    # Consumers of the hub (and their ffmpeg children) first, then ingest itself.
    await stream_manager.stop_all()
    await hls_manager.stop_all()
    await motion_lifecycle.stop_all()
    await snapshot.stop_all()
    await ingest.stop_all()
    # Stop Pulsar consumers first, then close every TuyaCloudClient.
    await tuya_events.stop_all()
    await tuya_client_pool.close_all()
    if settings.HLS_ENABLED:
        await hls_manager.stop_cleanup_task()
    if settings.WEBRTC_ENABLED:
        await webrtc_manager.stop_all_sessions()
    await motion_cleanup.stop()
    await auth.stop_session_cleanup()


def create_app() -> FastAPI:
    # Setup logging first
    setup_logging()

    app = FastAPI(
        title="ZeroCams",
        lifespan=lifespan,
        docs_url="/v1/docs",
        redoc_url=None,
        openapi_url="/v1/openapi.json",
    )

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    app.add_middleware(CursedMiddleware)

    @app.middleware("http")
    async def _default_cache_headers(request: Request, call_next):
        response = await call_next(request)
        if "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.include_router(service.router)
    app.include_router(config.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(sessions.router)
    app.include_router(cams.router)
    app.include_router(switches.router)
    app.include_router(connections.router)
    app.include_router(invitations.router)
    app.include_router(shares.router)
    app.include_router(push_settings.router)
    app.include_router(motion.router)
    app.include_router(stats.router)
    app.include_router(webrtc.router)

    return app


app = create_app()
