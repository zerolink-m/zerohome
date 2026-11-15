"""Bounded startup warm-up: pay lazy-initialization costs before the first real request.

Side-effect free: ``SELECT 1`` plus unauthenticated GETs sent in-process through the ASGI
transport (no sockets, no tokens, no writes). Never raises; the whole run is bounded by
``STARTUP_WARMUP_TIMEOUT_SECONDS``.

Rate limiting keys clients by ``resolve_ip`` (peer host, or x-real-ip only when the peer is
private). The self-requests use the fake peer ``("warmup", 0)``: not a valid/private IP, so
x-real-ip is ignored and they only touch their own ``*:warmup`` buckets, never a real
client's (including 127.0.0.1). Those buckets are removed afterwards.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from sqlalchemy import text

from app.core import rate_limit
from app.core.config import settings
from app.core.response import listing
from app.db import session as db_session

log = logging.getLogger(__name__)

WARMUP_CLIENT = ("warmup", 0)
DB_CONNECTIONS = 2
PATHS = ("/v1/health", "/v1/version", "/v1/config/time", "/v1/cams")  # last one: 401 expected


async def _warm_db() -> None:
    async def one() -> None:
        async with db_session.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            await asyncio.sleep(0)  # keep both connections checked out together

    await asyncio.gather(*(one() for _ in range(DB_CONNECTIONS)))


async def _warm_http(app) -> None:
    transport = httpx.ASGITransport(app=app, client=WARMUP_CLIENT)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://warmup.invalid") as client:
            for path in PATHS:
                await client.get(path)  # statuses (incl. 401) are not failures
    finally:
        for key in [k for k in rate_limit._buckets if k.endswith(":warmup")]:
            rate_limit._buckets.pop(key, None)


async def _warm_serialization() -> None:
    listing("cams", [])
    listing("switches", [])


async def _run_steps(app, durations: dict[str, float], failures: list[str]) -> None:
    steps = (("db", lambda: _warm_db()), ("http", lambda: _warm_http(app)),
             ("serialization", lambda: _warm_serialization()))
    for name, factory in steps:
        started = time.perf_counter()
        try:
            await factory()
        except asyncio.CancelledError:
            durations[name] = (time.perf_counter() - started) * 1000
            raise
        except Exception as exc:
            failures.append(f"{name}={type(exc).__name__}")
        durations[name] = (time.perf_counter() - started) * 1000


async def run(app=None) -> None:
    durations: dict[str, float] = {}
    failures: list[str] = []
    started = time.perf_counter()
    try:
        if app is None:
            from app.main import app as main_app
            app = main_app
        await asyncio.wait_for(
            _run_steps(app, durations, failures),
            timeout=settings.STARTUP_WARMUP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning("startup warm-up timed out after %.1fs (%s)",
                    settings.STARTUP_WARMUP_TIMEOUT_SECONDS, _fmt(durations))
        return
    except Exception as exc:
        log.warning("startup warm-up failed: %s", type(exc).__name__)
        return
    total = (time.perf_counter() - started) * 1000
    if failures:
        log.warning("startup warm-up finished with failures [%s] total_ms=%.0f %s",
                    ",".join(failures), total, _fmt(durations))
    else:
        log.info("startup warm-up done total_ms=%.0f %s", total, _fmt(durations))


def _fmt(durations: dict[str, float]) -> str:
    return " ".join(f"{k}_ms={v:.0f}" for k, v in durations.items())
