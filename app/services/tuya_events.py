"""Real-time device events from Tuya via the Pulsar message queue.

This runs *alongside* the polling worker (`switch_sync.py`), not instead of
it. Pulsar gives us sub-second reaction to state changes (physical button,
Tuya app, schedules) instead of the 5-minute polling lag; polling stays on
as a safety net for when the subscription drops, reconnects, or misses a
message during a restart.

One `TuyaPulsarConsumer` is started per enabled Tuya `Connection`. The topic
is per developer-account, so all devices of one account arrive on one stream.
Consumers whose region is not Pulsar-capable (`us-e`, `eu-w`, `sg` — the
`TuyaPulsarConsumer` only knows `cn/us/eu/in`) are skipped with a warning;
those connections rely on polling alone.

Event handling mirrors `switch_sync._sync_one`: on a DP-report we locate the
matching `Switch` rows, update their `state` if changed, and fire the same
push notification. The handler is idempotent (no-op when state is unchanged),
which also absorbs the echo of our own `send_commands` calls and any Pulsar
redeliveries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.connection import Connection
from app.models.switch import Switch
from app.services import push
from app.schemas.connection import TUYA_ENDPOINTS

logger = logging.getLogger(__name__)

# region-code → openapi host, reversed so we can map a stored endpoint back to
# the region string TuyaPulsarConsumer expects. Only these four regions have a
# Pulsar broker in the client; the rest are polling-only.
_HOST_TO_REGION: dict[str, str] = {host: code for code, host in TUYA_ENDPOINTS.items()}
_PULSAR_REGIONS = frozenset({"cn", "us", "eu", "in"})

_task: asyncio.Task | None = None
# connection_id → running consumer task, so we can restart/stop individually.
_consumers: dict[int, asyncio.Task] = {}


def _pulsar_region(endpoint: str) -> str | None:
    """Map a stored endpoint (host or region code) to a Pulsar region code,
    or None if this region has no Pulsar broker."""
    region = endpoint if endpoint in _PULSAR_REGIONS else _HOST_TO_REGION.get(endpoint)
    if region in _PULSAR_REGIONS:
        return region
    return None


def _switch_label(switch: Switch) -> str:
    return switch.name or str(switch.id)


async def _apply_dp_report(conn_id: int, dev_id: str, status: list[dict]) -> None:
    """Update Switch rows for one device from a Pulsar DP-report.

    A report carries one or more `{code, value}` entries. We update every
    matching child row (external_switch_channel == code) and recompute the
    master (OR across its channels), pushing notifications where the state
    actually changed — same contract as the polling worker.
    """
    # Only boolean-ish DP codes we track as channels matter here.
    changes: dict[str, bool] = {}
    for entry in status:
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        if isinstance(code, str):
            changes[code] = bool(entry.get("value"))
    if not changes:
        return

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Switch).where(
                    Switch.connection_id == conn_id,
                    Switch.external_id == dev_id,
                    Switch.added_by == "auto",
                )
            )
        ).scalars().all()
        if not rows:
            return

        now = int(time.time())
        masters = [r for r in rows if r.is_master == 1]
        children = [r for r in rows if r.is_master == 0]
        to_notify: list[Switch] = []

        # ---- children: direct DP match ----
        for child in children:
            code = child.external_switch_channel
            if code not in changes:
                continue
            new_state = 1 if changes[code] else 0
            if child.state != new_state:
                child.state = new_state
                child.state_updated_at = now
                if child.notify == 1:
                    to_notify.append(child)

        # ---- master: OR across its channels' current child states ----
        for master in masters:
            channel_states = [
                c.state for c in children if c.external_id == master.external_id
            ]
            if not channel_states:
                # No child rows (shouldn't happen for a synced master) — fall
                # back to the raw report's switch_* codes.
                channel_states = [
                    1 if v else 0
                    for k, v in changes.items()
                    if k.startswith("switch_") and k[len("switch_"):].isdigit()
                ]
            if not channel_states:
                continue
            new_state = 1 if any(s == 1 for s in channel_states) else 0
            if master.state != new_state:
                master.state = new_state
                master.state_updated_at = now
                if master.notify == 1:
                    to_notify.append(master)

        await db.commit()

        # Notify with timestamp
        from datetime import datetime
        now_str = datetime.fromtimestamp(now).strftime("%d.%m.%Y %H:%M:%S")

        for sw in to_notify:
            message = (
                f"🔌 Устройство {_switch_label(sw)} изменило состояние: "
                f"{'включено' if sw.state == 1 else 'выключено'}\n\n"
                f"Время: {now_str}"
            )
            await push._notify_recipients_filtered(
                db, "switches", sw.id, sw.ownuser, message,
                filter_func=lambda ps: ps.notify_state_change == 1
            )
            logger.info(
                "switch_id=%s state -> %s via pulsar", sw.id, sw.state
            )


def _make_handler(conn_id: int):
    async def _handler(event: dict[str, Any]) -> None:
        dev_id = event.get("devId")
        if not isinstance(dev_id, str) or not dev_id:
            return
        status = event.get("status")
        if isinstance(status, list):
            await _apply_dp_report(conn_id, dev_id, status)
            return
        # Business lifecycle events (online/offline/nameUpdate/...) — just log
        # for now; polling still catches state, and inventory is refreshed by
        # connection_sync.
        biz = event.get("bizCode")
        if biz:
            logger.info(
                "pulsar biz event connection_id=%s dev=%s bizCode=%s",
                conn_id, dev_id, biz,
            )
    return _handler


async def _run_consumer(conn_id: int, access_id: str, access_secret: str, region: str) -> None:
    """Run one connection's consumer until cancelled, reconnecting on failure."""
    # Imported here so the module (and the whole app) still loads when
    # pulsar-client / pycryptodome aren't installed — only starting a consumer
    # requires them.
    from app.services.tuya import TuyaPulsarConsumer

    handler = _make_handler(conn_id)
    while True:
        consumer = None
        try:
            consumer = TuyaPulsarConsumer(
                access_id=access_id,
                access_secret=access_secret,
                region=region,  # type: ignore[arg-type]
            )
            logger.info("pulsar consumer starting for connection_id=%s region=%s", conn_id, region)
            await consumer.run(handler)  # blocks until stop()/error
        except asyncio.CancelledError:
            if consumer is not None:
                await consumer.stop()
            raise
        except Exception:
            logger.exception(
                "pulsar consumer for connection_id=%s crashed; retrying in 30s", conn_id
            )
            await asyncio.sleep(30)


async def _supervise() -> None:
    """Start a consumer per enabled Tuya connection with a Pulsar-capable
    region. Started once at boot; individual consumers self-heal via their own
    reconnect loop. Not a periodic reconciler — connection add/remove during
    runtime is handled by restarting the app or the router hooks (future)."""
    async with AsyncSessionLocal() as db:
        conns = (
            await db.execute(select(Connection).where(Connection.enabled == 1))
        ).scalars().all()

    for conn in conns:
        if conn.provider != "tuya_cloud":
            continue
        m = conn.meta or {}
        access_id = m.get("access_id")
        access_secret = m.get("access_secret")
        endpoint = m.get("endpoint")
        if not (access_id and access_secret and endpoint):
            continue
        region = _pulsar_region(endpoint)
        if region is None:
            logger.warning(
                "connection_id=%s endpoint=%s has no Pulsar broker; "
                "relying on polling only", conn.id, endpoint,
            )
            continue
        if conn.id in _consumers and not _consumers[conn.id].done():
            continue
        _consumers[conn.id] = asyncio.create_task(
            _run_consumer(conn.id, access_id, access_secret, region)
        )


def start() -> None:
    """Launch the Pulsar supervisor. No-op if pulsar deps are missing — the
    supervisor will simply log the crash of each consumer and back off, which
    is harmless (polling keeps working)."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_supervise())


async def stop_all() -> None:
    """Cancel the supervisor and every running consumer. Called from FastAPI shutdown
    and by the cluster runner when the right is lost. Idempotent."""
    global _task
    tasks = list(_consumers.values())
    _consumers.clear()
    if _task is not None and not _task.done():
        tasks.append(_task)
    _task = None
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            # Expected — this is how each consumer task exits after .cancel().
            pass
        except Exception:
            logger.exception("pulsar consumer raised during shutdown")
