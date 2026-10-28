"""Owned ffmpeg/ffprobe children of WebRTC sessions.

Same safety model as the ingest package (ownership.py): every child starts in its own
session with the ZEROCAMS_INGEST tag in its environment; with INGEST_ENABLED it is also
written to its own registry file (webrtc_registry.json) so a crashed app's leftovers are
reaped at the next start. Processes are only ever killed through ownership checks
(registry start time + env tag + binary), never found by name, IP or URL."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.services.ingest import ownership

logger = logging.getLogger(__name__)

TERM_TIMEOUT_SECONDS = 2.0
KILL_TIMEOUT_SECONDS = 2.0

_registry: ownership.Registry | None = None
_slots = itertools.count(1)  # registry key: many decoders may run for one camera
_live: set["OwnedProcess"] = set()
_background: set[asyncio.Task] = set()


def hold(task: asyncio.Task) -> None:
    """Keeps a strong reference to a fire-and-forget termination task until it is done."""
    _background.add(task)
    task.add_done_callback(_background.discard)


def registry_path() -> Path:
    return Path(settings.ingest_registry_path).with_name("webrtc_registry.json")


def _ensure_registry() -> ownership.Registry | None:
    global _registry
    if not settings.INGEST_ENABLED:
        return None
    path = registry_path()
    if _registry is None or _registry.path != path:
        _registry = ownership.Registry(path)
        _registry.save()
    return _registry


async def startup() -> None:
    """Reaps decoders left by a previous run and opens this run's registry. No-op
    unless INGEST_ENABLED. Call once at app startup, before any session."""
    global _registry
    if not settings.INGEST_ENABLED:
        return
    try:
        report = await ownership.reap_orphans(registry_path(), binary=settings.FFMPEG_BINARY)
        logger.info("webrtc: decoder orphan sweep: %s", report)
    except Exception:
        logger.exception("webrtc: decoder orphan sweep failed")
    _registry = None
    _ensure_registry()


@dataclass(eq=False)
class OwnedProcess:
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
        try:
            proc = self.process
            if proc.returncode is None:
                outcome = await ownership.kill_tree_safely(
                    proc.pid, start_time=self.start_time, binary=self.binary, tag=self.tag,
                    term_timeout=TERM_TIMEOUT_SECONDS, kill_timeout=KILL_TIMEOUT_SECONDS,
                )
                if outcome == "not_ours" and proc.returncode is None:
                    # /proc unreadable: still our own unreaped child
                    logger.warning("webrtc: cannot verify pid %s via /proc, killing via handle", proc.pid)
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
        finally:
            self._forget()

    def kill_now(self) -> None:
        """Synchronous last resort (shutdown budget exhausted); still ownership-checked."""
        ownership.kill_now(self.pid, start_time=self.start_time, binary=self.binary, tag=self.tag)
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
        self._forget()

    def _forget(self) -> None:
        _live.discard(self)
        if self.registered and _registry is not None:
            _registry.remove(self.slot, self.pid)
            self.registered = False


async def spawn(args: list[str], *, cam_id: int, stdin: bool) -> OwnedProcess:
    """Starts `args` as an owned child (stdout/stderr piped; stdin piped if asked)."""
    registry = _ensure_registry()
    slot = next(_slots)
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
        process=process, slot=slot, cam_id=cam_id, tag=tag, binary=args[0],
        start_time=ownership.read_start_time(process.pid),
    )
    if registry is not None and owned.start_time is not None:
        registry.add(slot, process.pid, owned.start_time)
        owned.registered = True
    _live.add(owned)
    return owned


def force_kill_all() -> None:
    for owned in list(_live):
        owned.kill_now()
