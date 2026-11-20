"""Feed the local ingest hub from the node that holds the ingest lease.

Local consumers (HLS, WebRTC, snapshot, motion, /stream) keep subscribing to the
hub. This module is the only place that decides the source:

* this node holds ingest -> ffmpeg feeds the hub, no remote subscribe;
* it does not -> the media client feeds the hub, and only while somebody local
  is watching (subscriber_count > 0) plus CLUSTER_MEDIA_UNSUBSCRIBE_GRACE_SECONDS
  after the last viewer leaves.

Consumers call supervisor.is_running before they wait for the hub to come online,
and they subscribe only after that. note_demand() is that call: it counts as
interest so the pull can start before subscriber_count goes above zero. With the
bridge stopped (CLUSTER_ENABLED=false) note_demand is false and is_running is
unchanged.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Callable

from sqlalchemy import select

from app.core.config import settings as app_settings
from app.db import session as db_session
from app.models.node import Assignment, Node
from app.services.cluster.media_client import MediaClient
from app.services.ingest.hub import STATE_CONNECTING, STATE_OFFLINE, STATE_ONLINE, IngestHub

logger = logging.getLogger(__name__)

Holds = Callable[[int], bool]


class RemoteHub:
    def __init__(
        self,
        *,
        runner: Any = None,
        settings: Any = None,
        sessionmaker: Any = None,
        hub: IngestHub | None = None,
        node_id: str | None = None,
        holds: Holds | None = None,
        client: MediaClient | None = None,
        grace: float | None = None,
        watch_interval: float = 2.0,
        reconnect_min: float = 0.5,
        reconnect_max: float = 5.0,
    ) -> None:
        self.settings = settings or app_settings
        self._sessionmaker = sessionmaker
        self._runner = runner
        self._hub = hub
        self.node_id = node_id or (runner.node_id if runner is not None else "")
        if holds is not None:
            self._holds = holds
        elif runner is not None:
            self._holds = lambda cam_id: cam_id in runner.held("ingest")
        else:
            self._holds = lambda _cam_id: False
        self._grace = float(self.settings.CLUSTER_MEDIA_UNSUBSCRIBE_GRACE_SECONDS if grace is None else grace)
        self._watch = watch_interval
        self._client = client
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self._interest: dict[int, float] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._remote: set[int] = set()
        self._generation: dict[int, tuple[str, int, str]] = {}
        self.running = False

    def hub(self) -> IngestHub:
        if self._hub is None:
            from app.services.ingest import get_hub
            self._hub = get_hub()
        return self._hub

    def _ensure_client(self) -> MediaClient:
        if self._client is None:
            self._client = MediaClient(
                node_id=self.node_id,
                secret=self.settings.CLUSTER_SECRET,
                default_port=int(self.settings.CLUSTER_MEDIA_PORT),
                on_init=self._on_init,
                on_fragment=self._on_fragment,
                on_reset=self._on_reset,
                on_state=self._on_state,
                on_error=self._on_error,
                reconnect_min=self._reconnect_min,
                reconnect_max=self._reconnect_max,
            )
        return self._client

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._ensure_client()
        self.hub().add_viewers_listener(self._on_viewers)

    async def stop(self) -> None:
        self.running = False
        with contextlib.suppress(Exception):
            self.hub().remove_viewers_listener(self._on_viewers)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=2.0)
        self._tasks.clear()
        self._remote.clear()
        self._interest.clear()
        self._generation.clear()
        if self._client is not None:
            await self._client.close()

    def has_local_viewers(self, cam_id: int) -> bool:
        return self.hub().subscriber_count(cam_id) > 0

    def note_demand(self, cam_id: int) -> bool:
        """A local consumer is about to wait for this camera. True if we will pull it."""
        if not self.running or self._holds(cam_id):
            return False
        self._interest[cam_id] = time.monotonic()
        self._kick(cam_id)
        return True

    async def on_acquired(self, cam_id: int) -> None:
        """We now own ingest. Stop the pull before local ffmpeg starts."""
        was_remote = cam_id in self._remote or cam_id in self._generation
        self._interest.pop(cam_id, None)
        self._generation.pop(cam_id, None)
        task = self._tasks.pop(cam_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=2.0)
        self._remote.discard(cam_id)
        if self._client is not None:
            await self._client.unsubscribe(cam_id)
        if was_remote:
            hub = self.hub().peek(cam_id)
            if hub is not None:
                hub.reset_stream("local-ingest")

    async def on_lost(self, cam_id: int) -> None:
        """Ingest moved away. The runner calls this only when local viewers existed
        before ffmpeg was stopped (that stop already reset them off the hub)."""
        if not self.running:
            return
        self._interest[cam_id] = time.monotonic()
        self._kick(cam_id)

    def _on_viewers(self, cam_id: int, count: int) -> None:
        if not self.running or self._holds(cam_id):
            return
        if count > 0:
            self._interest[cam_id] = time.monotonic()
            self._kick(cam_id)
            return
        # Grace starts now, so a short viewer gap does not tear the stream down.
        self._interest[cam_id] = time.monotonic()
        self._kick(cam_id)

    def _interested(self, cam_id: int) -> bool:
        if not self.running or self._holds(cam_id):
            return False
        if self.hub().subscriber_count(cam_id) > 0:
            return True
        started = self._interest.get(cam_id)
        return started is not None and (time.monotonic() - started) < self._grace

    def _kick(self, cam_id: int) -> None:
        task = self._tasks.get(cam_id)
        if task is not None and not task.done():
            return
        self._tasks[cam_id] = asyncio.create_task(self._run_cam(cam_id), name=f"cluster-remote-{cam_id}")

    async def _run_cam(self, cam_id: int) -> None:
        client = self._ensure_client()
        try:
            while self._interested(cam_id):
                target = await self._resolve(cam_id)
                known = self._generation.get(cam_id)
                if target is None:
                    await asyncio.sleep(min(0.5, self._watch))
                    continue
                if target != known:
                    if known is not None:
                        self._reset(cam_id, "generation")
                        await client.unsubscribe(cam_id)
                    owner, generation, address = target
                    # Mark first, so bytes that arrive during subscribe are not dropped.
                    self._remote.add(cam_id)
                    try:
                        await client.subscribe(cam_id, owner, address)
                    except Exception:
                        self._remote.discard(cam_id)
                        logger.warning("cluster media: subscribe cam %s via %s failed", cam_id, owner, exc_info=True)
                        await asyncio.sleep(min(0.5, self._watch))
                        continue
                    self._generation[cam_id] = target
                    logger.info("cluster media: cam %s pulled from %s generation %s", cam_id, owner, generation)
                for _ in range(max(1, int(self._watch / 0.05))):
                    if not self._interested(cam_id) or self._generation.get(cam_id) != target:
                        break
                    await asyncio.sleep(0.05)
            await client.unsubscribe(cam_id)
        except asyncio.CancelledError:
            raise
        finally:
            self._remote.discard(cam_id)
            self._generation.pop(cam_id, None)
            self._tasks.pop(cam_id, None)

    async def _resolve(self, cam_id: int) -> tuple[str, int, str] | None:
        """(owner node id, generation, address) of the live ingest owner, or None."""
        if self._holds(cam_id):
            return None
        now = int(time.time())
        sm = self._sessionmaker or db_session.AsyncSessionLocal
        async with sm() as db:
            row = (await db.execute(select(Assignment).where(
                Assignment.function == "ingest",
                Assignment.resource_id == cam_id,
                Assignment.expires_at > now,
            ))).scalar_one_or_none()
            if row is None or not row.owner_node_id or row.owner_node_id == self.node_id:
                return None
            owner = row.owner_node_id
            generation = int(row.generation)
            node = await db.get(Node, owner)
            address = (node.address or "").strip() if node is not None else ""
        if not address:
            return None
        return owner, generation, address

    def _reset(self, cam_id: int, reason: str) -> None:
        hub = self.hub().peek(cam_id)
        if hub is not None:
            hub.reset_stream(reason or "remote-reset")

    def _serving(self, cam_id: int) -> bool:
        return cam_id in self._remote and not self._holds(cam_id)

    def _on_init(self, cam_id: int, data: bytes) -> None:
        if not self._serving(cam_id) or not data:
            return
        self.hub().get(cam_id).feed_remote(init=data)

    def _on_fragment(self, cam_id: int, data: bytes) -> None:
        if not self._serving(cam_id) or not data:
            return
        self.hub().get(cam_id).feed_remote(fragment=data)

    def _on_reset(self, cam_id: int, reason: str) -> None:
        if cam_id not in self._remote and cam_id not in self._generation:
            return
        self._reset(cam_id, reason)
        if reason == "not_owner":
            # Force the loop to read the lease again and follow the new owner.
            self._generation.pop(cam_id, None)

    def _on_state(self, cam_id: int, state: str) -> None:
        if not self._serving(cam_id):
            return
        hub = self.hub().get(cam_id)
        if state == "online":
            if hub.init_segment is not None:
                hub.set_state(STATE_ONLINE)
            return
        if state == "starting":
            hub.set_state(STATE_CONNECTING)
            return
        if state == "offline":
            hub.set_state(STATE_OFFLINE)

    def _on_error(self, cam_id: int, code: str) -> None:
        logger.info("cluster media: cam %s remote error %s", cam_id, code)
        if code == "not_owner":
            self._generation.pop(cam_id, None)


_bridge: RemoteHub | None = None


def get_bridge() -> RemoteHub | None:
    return _bridge


def set_bridge(bridge: RemoteHub | None) -> None:
    global _bridge
    _bridge = bridge


def note_demand(cam_id: int) -> bool:
    """is_running() hook. False when cluster media is not running."""
    bridge = _bridge
    if bridge is None:
        return False
    try:
        return bridge.note_demand(cam_id)
    except Exception:
        logger.exception("cluster media: demand for cam %s failed", cam_id)
        return False


async def stop() -> None:
    bridge = get_bridge()
    set_bridge(None)
    if bridge is not None:
        await bridge.stop()
