"""Helpers for consumers of the ingest hub that run their own local ffmpeg (stage 5:
snapshots and motion detection).

`OwnedProcessPool` is the same safety model as ownership.py / webrtc_manager/process.py:
every child starts in its own session with the ZEROCAMS_INGEST tag in its environment;
with INGEST_ENABLED it is also written to the pool's own registry file so a crashed app's
leftovers are reaped at the next start. Processes are only ever killed through ownership
checks (registry start time + env tag + binary), never found by name, IP or URL.

`ready_hub` is the "is the hub usable for this camera right now" check."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings
from app.services.ingest import ownership

logger = logging.getLogger(__name__)


async def ready_hub(cam_id: int, wait_seconds: float, need_gop: bool = False):
    """The camera's CamHub when ingest is on, running for it, online and fed recently
    (waiting up to `wait_seconds` for that), else None. `need_gop`: also wait until at
    least one whole fragment is stored (a snapshot needs frames, not just the init)."""
    if not settings.INGEST_ENABLED:
        return None
    from app.services.ingest import get_hub, get_supervisor  # late: avoids import cycles

    try:
        if not get_supervisor().is_running(cam_id):
            return None
        hub = get_hub().get(cam_id)
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            fresh = (
                hub.last_data_at is not None
                and time.monotonic() - hub.last_data_at <= settings.INGEST_STALL_SECONDS
            )
            if hub.online.is_set() and hub.init_segment is not None and fresh and (hub.gop or not need_gop):
                return hub
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.05)
    except Exception:
        logger.exception("ingest: hub check failed for cam %s", cam_id)
        return None


@dataclass(eq=False)
class OwnedProcess:
    pool: "OwnedProcessPool"
    process: asyncio.subprocess.Process
    slot: int
    cam_id: int
    tag: str
    binary: str
    start_time: int | None
    registered: bool = False

    @property
    def pid(self) -> int:
        return self.process.pid

    async def terminate(self) -> None:
        """SIGTERM then SIGKILL of our group, ownership-checked. Idempotent."""
        proc = self.process
        try:
            if proc.returncode is None:
                outcome = await ownership.kill_tree_safely(
                    proc.pid, start_time=self.start_time, binary=self.binary, tag=self.tag,
                    term_timeout=self.pool.term_timeout, kill_timeout=self.pool.kill_timeout,
                )
                if outcome == "not_ours" and proc.returncode is None:
                    # /proc unreadable: still our own unreaped child
                    logger.warning("%s: cannot verify pid %s via /proc, killing via handle", self.pool.name, proc.pid)
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
        finally:
            self.forget()

    def kill_now(self) -> None:
        """Synchronous last resort (shutdown budget exhausted); still ownership-checked."""
        ownership.kill_now(self.pid, start_time=self.start_time, binary=self.binary, tag=self.tag)
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
        self.forget()

    def terminate_soon(self) -> asyncio.Task | None:
        """Fire-and-forget terminate() that survives the caller being cancelled."""
        try:
            task = asyncio.get_running_loop().create_task(self.terminate())
        except RuntimeError:
            self.kill_now()  # no loop: kill at once rather than leak the process
            return None
        self.pool.hold(task)
        return task

    def forget(self) -> None:
        self.pool.live.discard(self)
        registry = self.pool.registry
        if self.registered and registry is not None:
            registry.remove(self.slot, self.pid)
            self.registered = False


@dataclass
class OwnedProcessPool:
    """Owned ffmpeg children of one consumer kind, with a registry file of its own
    (`registry_file`, next to the ingest registry)."""
    name: str
    registry_file: str
    term_timeout: float = 2.0
    kill_timeout: float = 2.0
    registry: ownership.Registry | None = None
    live: set[OwnedProcess] = field(default_factory=set)
    _slots: "itertools.count[int]" = field(default_factory=lambda: itertools.count(1))
    _background: set[asyncio.Task] = field(default_factory=set)

    def hold(self, task: asyncio.Task) -> None:
        """Keeps a strong reference to a fire-and-forget termination task until done."""
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def registry_path(self) -> Path:
        return Path(settings.ingest_registry_path).with_name(self.registry_file)

    def _ensure_registry(self) -> ownership.Registry | None:
        if not settings.INGEST_ENABLED:
            return None
        path = self.registry_path()
        if self.registry is None or self.registry.path != path:
            self.registry = ownership.Registry(path)
            self.registry.save()
        return self.registry

    async def startup(self) -> None:
        """Reaps children left by a previous run and opens this run's registry. No-op
        unless INGEST_ENABLED. Call once at app startup, before any consumer."""
        if not settings.INGEST_ENABLED:
            return
        try:
            report = await ownership.reap_orphans(self.registry_path(), binary=settings.FFMPEG_BINARY)
            logger.info("%s: orphan sweep: %s", self.name, report)
        except Exception:
            logger.exception("%s: orphan sweep failed", self.name)
        self.registry = None
        self._ensure_registry()

    async def spawn(self, args: list[str], *, cam_id: int, stdin: bool) -> OwnedProcess:
        """Starts `args` as an owned child (stdout/stderr piped; stdin piped if asked)."""
        registry = self._ensure_registry()
        slot = next(self._slots)  # registry key: many children may run for one camera
        instance = registry.instance_id if registry is not None else ownership.new_instance_id()
        tag = ownership.make_tag(instance, slot)
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, ownership.ENV_TAG: tag},
            start_new_session=True,
        )
        owned = OwnedProcess(
            pool=self, process=process, slot=slot, cam_id=cam_id, tag=tag, binary=args[0],
            start_time=ownership.read_start_time(process.pid),
        )
        if registry is not None and owned.start_time is not None:
            registry.add(slot, process.pid, owned.start_time)
            owned.registered = True
        self.live.add(owned)
        return owned

    def force_kill_all(self) -> None:
        for owned in list(self.live):
            owned.kill_now()

    async def stop_all(self, timeout: float = 6.0) -> None:
        """App shutdown: ends every live child within `timeout` overall (then SIGKILL)."""
        live = list(self.live)
        if not live:
            return
        tasks = [asyncio.create_task(o.terminate()) for o in live]
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning("%s: shutdown budget exceeded, force-killing the rest", self.name)
            self.force_kill_all()
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=1.0)
