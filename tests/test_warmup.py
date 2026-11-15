"""Startup warm-up: bounded, never raises, side-effect free."""
import asyncio
import importlib
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlalchemy import select

from app.core import rate_limit
from app.core.config import Settings, settings
from app.models.session import Session
from app.services import warmup

main = importlib.import_module("app.main")


def _stub_lifespan_deps(monkeypatch):
    monkeypatch.setattr(main, "start_cleanup_task", Mock())
    for flag in ("SESSION_CLEANUP_ENABLED", "HEALTHCHECK_ENABLED", "SWITCH_SYNC_ENABLED",
                 "CONNECTION_SYNC_ENABLED", "TUYA_EVENTS_ENABLED", "MOTION_CLEANUP_ENABLED",
                 "HLS_ENABLED", "WEBRTC_ENABLED"):
        monkeypatch.setattr(settings, flag, False)
    for module, names in [
        (main.session_activity, ("start",)),
        (main.ingest, ("start_all_enabled", "stop_all")),
        (main.stream_manager, ("startup", "stop_all")),
        (main.hls_manager, ("startup", "stop_all")),
        (main.webrtc_manager, ("startup",)),
        (main.snapshot, ("startup", "stop_all")),
        (main.motion_detection, ("startup",)),
        (main.motion_lifecycle, ("start_all_enabled", "stop_all")),
        (main.tuya_events, ("stop_all",)),
        (main.tuya_client_pool, ("close_all",)),
        (main.motion_cleanup, ("stop",)),
        (main.auth, ("stop_session_cleanup",)),
        (main.session_activity, ("stop",)),
    ]:
        for name in names:
            monkeypatch.setattr(module, name, AsyncMock() if name != "start" else Mock())


def test_defaults():
    assert Settings.model_fields["STARTUP_WARMUP_ENABLED"].default is True
    assert Settings.model_fields["STARTUP_WARMUP_TIMEOUT_SECONDS"].default == 10.0


async def test_disabled_flag_does_not_run(monkeypatch):
    _stub_lifespan_deps(monkeypatch)
    run = AsyncMock()
    monkeypatch.setattr(warmup, "run", run)
    monkeypatch.setattr(settings, "STARTUP_WARMUP_ENABLED", False)
    async with main.lifespan(main.app):
        pass
    run.assert_not_called()


async def test_enabled_flag_runs_once_before_yield(monkeypatch):
    _stub_lifespan_deps(monkeypatch)
    run = AsyncMock()
    monkeypatch.setattr(warmup, "run", run)
    monkeypatch.setattr(settings, "STARTUP_WARMUP_ENABLED", True)
    async with main.lifespan(main.app):
        run.assert_awaited_once_with(main.app)


async def test_failing_step_does_not_raise_and_later_steps_run(monkeypatch, app):
    monkeypatch.setattr(warmup, "_warm_db", AsyncMock(side_effect=RuntimeError("boom")))
    http, ser = AsyncMock(), AsyncMock()
    monkeypatch.setattr(warmup, "_warm_http", http)
    monkeypatch.setattr(warmup, "_warm_serialization", ser)
    await warmup.run(app)
    http.assert_awaited_once()
    ser.assert_awaited_once()


async def test_timeout_is_enforced_and_does_not_raise(monkeypatch, app):
    async def slow():
        await asyncio.sleep(30)

    monkeypatch.setattr(warmup, "_warm_db", slow)
    monkeypatch.setattr(settings, "STARTUP_WARMUP_TIMEOUT_SECONDS", 0.05)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await warmup.run(app)
    assert loop.time() - t0 < 2


async def test_self_requests_are_anonymous_and_leave_state_untouched(monkeypatch, app, db, engine):
    seen = []
    real = httpx.ASGITransport.handle_async_request

    async def spy(self, request):
        seen.append(request)
        return await real(self, request)

    monkeypatch.setattr(httpx.ASGITransport, "handle_async_request", spy)
    monkeypatch.setattr(warmup.db_session, "engine", engine)
    await warmup.run(app)
    assert [r.url.path for r in seen] == list(warmup.PATHS)
    assert all(r.method == "GET" and "authorization" not in r.headers for r in seen)
    assert not [k for k in rate_limit._buckets if k.endswith(":warmup")]
    async with db() as s:
        assert (await s.execute(select(Session))).first() is None
