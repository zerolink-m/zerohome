"""Rights for ingest, motion and global background functions (CLUSTER_ENABLED only).

Two background loops:
  * heartbeat: register the node, then refresh `nodes.heartbeat_at` periodically;
  * reconcile: every LEASE_RENEW_INTERVAL_SECONDS (or sooner after request_reconcile()),
    for each function renew held leases, release what is no longer wanted, and acquire
    free ones. Local work runs only while we hold the lease.

Per-camera functions ('ingest', 'motion'): one lease per camera, acquired up to a fair share.
Global functions ('healthcheck', 'switch_sync', 'connection_sync', 'tuya_events'): one lease
with resource_id 0; whoever takes it first runs the module's loop (start() every cycle is
idempotent), a node without the capability never takes it. No fair share for them.
Not under leases: 'session_cleanup' (idempotent, runs everywhere), 'motion_cleanup'
(deletes through the storage layer, idempotent, still runs on every node) and 'notify'
(its own loop takes that lease; see notify_worker).

Self-fencing: a lease that was not successfully renewed for TTL - RENEW seconds is dropped
locally (work stopped) before it can expire in the DB and be taken by another node.

Fair share: ceil(wanted cameras / live nodes with the capability). Cameras that stay without
an owner for a full renew interval may be taken beyond the share, so none stays unserved.
There is no rebalancing of cameras that are already owned.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from app.core.config import settings as app_settings
from app.db import session as db_session
from app.services.cluster import leases
from app.services.cluster.identity import get_node_id, local_capabilities

logger = logging.getLogger(__name__)

GLOBAL_RESOURCE_ID = 0
GLOBAL_FUNCTIONS = ("healthcheck", "switch_sync", "connection_sync", "tuya_events")
FUNCTIONS = ("ingest", "motion") + GLOBAL_FUNCTIONS


@dataclass
class ClusterFunction:
    name: str
    select: Callable[[], Awaitable[dict[int, Any]]]  # cam_id -> payload that wanted cameras carry
    start: Callable[[int, Any], Awaitable[bool]]  # idempotent; True when work is running
    stop: Callable[[int], Awaitable[None]]
    prepare: Callable[[], Awaitable[None]] | None = None
    scope: str = "camera"  # "camera" (resource_id = cam id) or "global" (resource_id 0)
    # payload -> comparable; when it changes for a held camera, local work is restarted.
    fingerprint: Callable[[Any], Any] | None = None


@dataclass
class _Held:
    lease: leases.Lease
    renewed_at: int
    task: asyncio.Task | None = None
    running: bool = False
    fp: Any = None


def cam_fingerprint(cam: Any) -> str:
    """Digest of the camera fields that affect a running motion session. A digest, so the
    credentials never leave this process."""
    parts = [cam.enabled, cam.name, cam.proto, cam.addr, cam.port, cam.path, cam.username, cam.password]
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def _global_function(name: str, start_fn: Callable[[], Any], stop_fn: Callable[[], Awaitable[None]]) -> ClusterFunction:
    async def select() -> dict[int, Any]:
        return {GLOBAL_RESOURCE_ID: None}

    async def start(_rid: int, _payload: Any) -> bool:
        start_fn()  # the modules' start() are sync, idempotent and only create the loop task
        return True

    async def stop(_rid: int) -> None:
        await stop_fn()

    return ClusterFunction(name, select, start, stop, scope="global")


def default_functions() -> list[ClusterFunction]:
    from app.models.cams import Cam
    from app.services import (connection_sync, healthcheck, motion_detection, motion_lifecycle,
                              switch_sync, tuya_events)
    from app.services.ingest import lifecycle as ingest_lifecycle

    async def ingest_start(cam_id: int, target: Any) -> bool:
        await ingest_lifecycle.get_supervisor().apply(cam_id, target)
        return True

    async def ingest_stop(cam_id: int) -> None:
        supervisor = ingest_lifecycle.peek_supervisor()
        if supervisor is not None:
            await supervisor.stop(cam_id)

    async def motion_select() -> dict[int, Any]:
        """cam_id -> fingerprint of the camera (a running session keeps its own copy of the
        camera row, so a changed camera needs a restart by the owner)."""
        ids = [cam_id for cam_id, _name in await motion_lifecycle.select_cams()]
        if not ids:
            return {}
        async with db_session.AsyncSessionLocal() as db:
            cams = (await db.execute(select(Cam).where(Cam.id.in_(ids)))).scalars().all()
        return {c.id: cam_fingerprint(c) for c in cams}

    async def motion_start(cam_id: int, _payload: Any) -> bool:
        return await motion_detection.start_motion_detection(cam_id)

    return [
        ClusterFunction("ingest", ingest_lifecycle.select_targets, ingest_start, ingest_stop,
                        prepare=ingest_lifecycle.prepare),
        ClusterFunction("motion", motion_select, motion_start, motion_detection.stop_motion_detection,
                        fingerprint=lambda payload: payload),
        _global_function("healthcheck", healthcheck.start, healthcheck.stop),
        _global_function("switch_sync", switch_sync.start, switch_sync.stop),
        _global_function("connection_sync", connection_sync.start, connection_sync.stop),
        _global_function("tuya_events", tuya_events.start, tuya_events.stop_all),
    ]


class ClusterRunner:
    def __init__(self, *, settings: Any = None, sessionmaker: Any = None,
                 clock: Callable[[], float] = time.time,
                 functions: list[ClusterFunction] | None = None, node_id: str | None = None) -> None:
        self.settings = settings or app_settings
        self._sessionmaker = sessionmaker
        self._clock = clock
        self._functions = functions
        self.node_id = node_id or get_node_id(self.settings)
        self._held: dict[tuple[str, int], _Held] = {}
        self._orphan_since: dict[tuple[str, int], int] = {}
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self._wake = asyncio.Event()

    # ----- helpers -----
    def _now(self) -> int:
        return int(self._clock())

    def _sm(self):
        return self._sessionmaker or db_session.AsyncSessionLocal

    def _funcs(self) -> list[ClusterFunction]:
        if self._functions is None:
            self._functions = default_functions()
        return self._functions

    @property
    def _ttl(self) -> int:
        return int(self.settings.LEASE_TTL_SECONDS)

    @property
    def _renew_interval(self) -> int:
        return int(self.settings.LEASE_RENEW_INTERVAL_SECONDS)

    async def _db(self, op: Callable[[Any], Awaitable[Any]]) -> Any:
        """One short DB operation in its own session, bounded by the renew interval."""
        async def _run():
            async with self._sm()() as db:
                return await op(db)
        return await asyncio.wait_for(_run(), timeout=max(1, self._renew_interval))

    def held(self, function: str) -> set[int]:
        return {rid for (fn, rid) in self._held if fn == function}

    def request_reconcile(self) -> None:
        """Makes the next reconcile cycle run soon (camera changed on this node). Never raises;
        does nothing when the runner is not running."""
        if self._started:
            self._wake.set()

    # ----- lifecycle -----
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        caps = local_capabilities(self.settings)
        for f in self._funcs():
            if f.prepare is not None and caps.get(f.name):
                await f.prepare()
        await self._heartbeat(register=True)
        try:
            await self.reconcile_once()
        except Exception:
            logger.exception("cluster: first reconcile failed")
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="cluster-heartbeat"),
            asyncio.create_task(self._reconcile_loop(), name="cluster-reconcile"),
        ]

    async def stop(self) -> None:
        """Stops the loops and releases every lease in the DB. Local ingest/motion work is
        stopped by the regular stop_all() calls that follow in the shutdown sequence."""
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5.0)
        self._started = False
        for f in self._funcs():
            if f.scope == "global":  # nothing later in the shutdown sequence stops these loops
                for rid in sorted(self.held(f.name)):
                    h = self._held[(f.name, rid)]
                    self._cancel_start(h)
                    try:
                        await f.stop(rid)
                    except Exception:
                        logger.exception("cluster: %s stop failed", f.name)
        for key, h in list(self._held.items()):
            self._cancel_start(h)
            try:
                await self._db(lambda db, lease=h.lease: leases.release(db, lease))
            except Exception:
                logger.warning("cluster: release %s/%s failed (lease will expire)", *key)
        self._held.clear()
        self._orphan_since.clear()

    # ----- heartbeat -----
    async def _heartbeat(self, register: bool = False) -> None:
        caps = local_capabilities(self.settings)
        fn = leases.register_node if register else leases.heartbeat
        try:
            await self._db(lambda db: fn(db, self.node_id, caps, self.settings.NODE_ADDRESS,
                                         self.settings.VERSION, self._now()))
        except Exception:
            logger.warning("cluster: node %s failed", "register" if register else "heartbeat", exc_info=True)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.NODE_HEARTBEAT_INTERVAL_SECONDS)
            await self._heartbeat()

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._renew_interval)
                await asyncio.sleep(0.5)  # coalesce a burst of camera changes
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("cluster: reconcile failed")

    # ----- local work -----
    def _cancel_start(self, h: _Held) -> None:
        if h.task is not None and not h.task.done():
            h.task.cancel()

    async def _drop(self, f: ClusterFunction, rid: int, *, release: bool) -> None:
        """Stop local work for the camera and forget the right (optionally release it)."""
        h = self._held.pop((f.name, rid), None)
        if h is None:
            return
        self._cancel_start(h)
        if h.task is not None:
            await asyncio.wait({h.task}, timeout=2.0)
        try:
            await f.stop(rid)
        except Exception:
            logger.exception("cluster: %s stop failed for cam %s", f.name, rid)
        if release:
            try:
                await self._db(lambda db: leases.release(db, h.lease))
            except Exception:
                logger.warning("cluster: release %s/%s failed (lease will expire)", f.name, rid)

    async def _restart_local(self, f: ClusterFunction, rid: int, h: _Held) -> None:
        """Stops local work but keeps the right; the caller schedules a fresh start."""
        self._cancel_start(h)
        if h.task is not None:
            await asyncio.wait({h.task}, timeout=2.0)
        h.task, h.running = None, False
        try:
            await f.stop(rid)
        except Exception:
            logger.exception("cluster: %s stop failed for cam %s", f.name, rid)

    async def _fence(self) -> None:
        """Drop every right that was not renewed within TTL - RENEW (DB unreachable or stuck)."""
        limit = self._ttl - self._renew_interval
        now = self._now()
        for f in self._funcs():
            for rid in sorted(self.held(f.name)):
                if now - self._held[(f.name, rid)].renewed_at >= limit:
                    logger.error("cluster: %s cam %s not renewed for %ss, stopping local work",
                                 f.name, rid, now - self._held[(f.name, rid)].renewed_at)
                    await self._drop(f, rid, release=False)

    def _schedule_start(self, f: ClusterFunction, rid: int, payload: Any) -> None:
        h = self._held[(f.name, rid)]
        if h.task is not None and not h.task.done():
            return

        async def _go() -> None:
            try:
                h.running = bool(await f.start(rid, payload))
            except asyncio.CancelledError:
                raise
            except Exception:
                h.running = False
                logger.exception("cluster: %s start failed for cam %s", f.name, rid)

        h.task = asyncio.create_task(_go(), name=f"cluster-{f.name}-{rid}")

    async def drain(self) -> None:
        """Waits for in-flight start tasks (tests, shutdown)."""
        tasks = {h.task for h in self._held.values() if h.task is not None and not h.task.done()}
        if tasks:
            await asyncio.wait(tasks)

    # ----- reconcile -----
    async def reconcile_once(self) -> None:
        await self._fence()
        for f in self._funcs():
            try:
                await self._reconcile_function(f)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("cluster: reconcile of %s failed", f.name, exc_info=True)
                await self._fence()

    async def _reconcile_function(self, f: ClusterFunction) -> None:
        if not local_capabilities(self.settings).get(f.name):
            for rid in sorted(self.held(f.name)):
                await self._drop(f, rid, release=True)
            return

        try:
            desired = await asyncio.wait_for(f.select(), timeout=max(1, self._renew_interval))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("cluster: %s camera selection failed", f.name, exc_info=True)
            desired = None  # keep what we have; only renew

        if desired is not None:
            for rid in sorted(self.held(f.name) - set(desired)):
                await self._drop(f, rid, release=True)  # deleted / disabled

        for rid in sorted(self.held(f.name)):
            h = self._held[(f.name, rid)]
            now = self._now()
            try:
                ok = await self._db(lambda db: leases.renew(db, h.lease, self._ttl, now))
            except Exception:
                logger.warning("cluster: renew %s/%s failed", f.name, rid, exc_info=True)
                continue
            if ok:
                h.renewed_at = now
            else:
                logger.warning("cluster: lost right %s/%s, stopping", f.name, rid)
                await self._drop(f, rid, release=False)

        if desired is not None:
            missing = [rid for rid in sorted(desired) if (f.name, rid) not in self._held]
            if missing:
                await self._acquire(f, desired, missing)

        if desired is not None:
            for rid in sorted(self.held(f.name)):
                if rid not in desired:
                    continue
                h = self._held[(f.name, rid)]
                if f.fingerprint is not None:
                    fp = f.fingerprint(desired[rid])
                    if h.fp is not None and h.fp != fp:
                        logger.info("cluster: %s cam %s changed, restarting local work", f.name, rid)
                        await self._restart_local(f, rid, h)
                    h.fp = fp
                self._schedule_start(f, rid, desired[rid])

    async def _acquire(self, f: ClusterFunction, desired: dict[int, Any], missing: list[int]) -> None:
        now = self._now()

        async def _load(db):
            rows = {a.resource_id: a for a in await leases.list_assignments(db) if a.function == f.name}
            try:
                nodes = await leases.list_nodes(db, now, 2 * int(self.settings.NODE_HEARTBEAT_INTERVAL_SECONDS))
            except Exception:
                nodes = None
            return rows, nodes

        rows, nodes = await self._db(_load)
        if nodes is None or f.scope == "global":
            share = len(desired)  # live nodes unknown / no fair share for global functions
        else:
            alive = {n.node_id for n in nodes if (n.capabilities or {}).get(f.name)} | {self.node_id}
            share = math.ceil(len(desired) / len(alive))

        mine: list[int] = []
        orphans: list[int] = []
        for rid in missing:
            key = (f.name, rid)
            row = rows.get(rid)
            if row is not None and row.owner_node_id == self.node_id and row.expires_at > now:
                mine.append(rid)  # our own live lease (e.g. restart with the same NODE_ID)
                self._orphan_since.pop(key, None)
            elif row is None or row.owner_node_id is None or row.expires_at <= now:
                orphans.append(rid)
                self._orphan_since.setdefault(key, now)
            else:
                self._orphan_since.pop(key, None)  # live foreign owner
        for key in [k for k in self._orphan_since if k[0] == f.name and k[1] not in desired]:
            del self._orphan_since[key]

        allowed = max(0, share - len(self.held(f.name)))
        order = [(rid, True) for rid in mine] + [(rid, False) for rid in orphans]
        for rid, is_mine in order:
            key = (f.name, rid)
            if not is_mine:
                unowned_long = now - self._orphan_since.get(key, now) >= self._renew_interval
                if allowed <= 0 and not unowned_long:
                    continue
            try:
                lease = await self._db(
                    lambda db: leases.try_acquire(db, f.name, rid, self.node_id, self._ttl, now))
            except Exception:
                logger.warning("cluster: acquire %s/%s failed", f.name, rid, exc_info=True)
                continue
            if lease is None:
                continue
            self._held[key] = _Held(lease=lease, renewed_at=now)
            self._orphan_since.pop(key, None)
            if not is_mine:
                allowed -= 1
            logger.info("cluster: acquired %s cam %s (generation %s)", f.name, rid, lease.generation)


_runner: ClusterRunner | None = None


async def start() -> None:
    global _runner
    if not app_settings.CLUSTER_ENABLED or _runner is not None:
        return
    _runner = ClusterRunner()
    try:
        await _runner.start()
    except Exception:
        logger.exception("cluster: failed to start")


def request_reconcile() -> None:
    """Module-level shortcut for the camera hooks: no-op unless the runner is running."""
    runner = _runner
    if runner is not None:
        try:
            runner.request_reconcile()
        except Exception:
            logger.exception("cluster: request_reconcile failed")


def get_runner() -> ClusterRunner | None:
    return _runner


async def stop() -> None:
    global _runner
    runner, _runner = _runner, None
    if runner is None:
        return
    try:
        await runner.stop()
    except Exception:
        logger.exception("cluster: failed to stop cleanly")
