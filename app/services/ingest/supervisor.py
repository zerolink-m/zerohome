"""Supervisor: one persistent ffmpeg per camera, kept alive and fed into the hub.

ffmpeg: RTSP(tcp) -> `-c copy` -> fragmented MP4 on stdout. Per camera a worker task
loops: spawn -> read until the process ends / stalls / a planned or requested restart
-> kill the group safely -> pause (growing backoff after failures) -> spawn again.
Process ownership rules live in ownership.py; nothing here signals a pid directly."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import time
from dataclasses import dataclass, field

from app.core.config import settings
from app.services.ingest import ownership
from app.services.ingest.config import IngestConfig
from app.services.ingest.hub import STATE_CONNECTING, STATE_OFFLINE, IngestHub
from app.services.rtsp_url import mask_credentials

logger = logging.getLogger(__name__)

_READ_CHUNK_SIZE = 64 * 1024
_RESPAWN_PAUSE = 0.2  # after a planned/requested restart (not a failure)


@dataclass
class IngestTarget:
    cam_id: int
    url: str = field(repr=False)  # contains the password: never log it
    name: str = ""


class _Worker:
    def __init__(self, target: IngestTarget):
        self.target = target
        self.task: asyncio.Task | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self.start_time: int | None = None
        self.tag: str = ""
        self.binary: str = ""
        self.started_at: float | None = None
        self.next_planned_at: float | None = None
        self.restart_event = asyncio.Event()
        self.last_stderr: str = ""
        self.backoff: float = 0.0


class IngestSupervisor:
    def __init__(self, hub: IngestHub | None = None, config: IngestConfig | None = None):
        self.config = config or IngestConfig.from_settings()
        self.hub = hub or IngestHub()
        self._workers: dict[int, _Worker] = {}
        self._lock = asyncio.Lock()
        self._registry: ownership.Registry | None = None

    # ------------------------------------------------------------------ lifecycle
    async def startup(self) -> dict[str, list[int]]:
        """Reaps verified orphans of the previous run and opens this run's registry."""
        report: dict[str, list[int]] = {"killed": [], "gone": [], "foreign": []}
        path = self.config.registry_path or settings.ingest_registry_path
        try:
            report = await ownership.reap_orphans(
                path, binary=settings.FFMPEG_BINARY, term_timeout=self.config.term_timeout_seconds
            )
        except Exception:
            logger.exception("ingest: orphan reaping failed")
        self._registry = ownership.Registry(path)
        self._registry.save()
        return report

    def _ensure_registry(self) -> ownership.Registry:
        if self._registry is None:
            path = self.config.registry_path or settings.ingest_registry_path
            self._registry = ownership.Registry(path)
            self._registry.save()
        return self._registry

    async def start(self, target: IngestTarget) -> None:
        await self.apply(target.cam_id, target)

    async def apply(self, cam_id: int, target: IngestTarget | None) -> str:
        """Idempotent reconfigure. None -> stop. Same URL -> no-op; changed URL ->
        restart (done by the worker, so this never waits for a process)."""
        if target is None:
            await self.stop(cam_id)
            return "stopped"
        async with self._lock:
            worker = self._workers.get(cam_id)
            if worker is None:
                worker = _Worker(target)
                self._ensure_registry()
                self._workers[cam_id] = worker
                worker.task = asyncio.create_task(self._run(worker), name=f"ingest-{cam_id}")
                logger.info("ingest: started cam %s", cam_id)
                return "started"
            if worker.task is None or worker.task.done():
                # The record is still here, but the task ended on its own (not via stop(),
                # which removes the record first). Same URL must not report "unchanged".
                self._retrieve_task_error(worker)
                worker.target = target
                worker.task = asyncio.create_task(self._run(worker), name=f"ingest-{cam_id}")
                logger.warning("ingest: cam %s worker died, restarting", cam_id)
                return "restarting"
            if worker.target.url != target.url:
                worker.target = target
                worker.restart_event.set()
                logger.info("ingest: cam %s settings changed, restarting", cam_id)
                return "restarting"
            worker.target = target
            return "unchanged"

    def _retrieve_task_error(self, worker: _Worker) -> None:
        task = worker.task
        if task is None or not task.done() or task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("ingest: cam %s worker exited: %s", worker.target.cam_id, exc)

    async def restart(self, cam_id: int) -> bool:
        worker = self._workers.get(cam_id)
        if worker is None:
            return False
        worker.restart_event.set()
        return True

    async def stop(self, cam_id: int, timeout: float | None = None) -> None:
        async with self._lock:
            worker = self._workers.pop(cam_id, None)
        if worker is not None:
            await self._stop_worker(worker, timeout)

    async def _stop_worker(self, worker: _Worker, timeout: float | None = None) -> None:
        timeout = self.config.stop_timeout_seconds if timeout is None else timeout
        task = worker.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=timeout)
            if not task.done():
                logger.warning("ingest: cam %s did not stop in %.1fs, killing", worker.target.cam_id, timeout)
                self._force_kill(worker)
                await asyncio.wait({task}, timeout=1.0)
        else:
            self._force_kill(worker)
        cam_hub = self.hub.peek(worker.target.cam_id)
        if cam_hub is not None:
            cam_hub.reset_stream("stopped")
        logger.info("ingest: stopped cam %s", worker.target.cam_id)

    async def stop_all(self, timeout: float | None = None) -> None:
        """Stops everything within `timeout` overall; whatever is left is SIGKILLed."""
        timeout = self.config.stop_timeout_seconds if timeout is None else timeout
        async with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        if not workers:
            return
        tasks = [asyncio.create_task(self._stop_worker(w, timeout)) for w in workers]
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning("ingest: shutdown budget exceeded, force-killing the rest")
            for w in workers:
                self._force_kill(w)
            for t in pending:
                t.cancel()
            await asyncio.wait(pending, timeout=1.0)

    def _force_kill(self, worker: _Worker) -> None:
        """Synchronous last resort; still goes through the ownership checks."""
        if worker.pid is not None:
            ownership.kill_now(
                worker.pid, start_time=worker.start_time, binary=worker.binary, tag=worker.tag
            )
        proc = worker.proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()  # our own child via its handle: pid cannot be recycled yet

    # ------------------------------------------------------------------ status
    def is_running(self, cam_id: int) -> bool:
        worker = self._workers.get(cam_id)
        if worker is not None:
            return worker.task is not None and not worker.task.done()
        # No local ffmpeg. Consumers (ready_hub, HLS, WebRTC, /stream) check this
        # before they wait for the hub, and only subscribe after it is online, so
        # the check itself is the demand for a remote pull. note_demand is false
        # when cluster media is off, including CLUSTER_ENABLED=false, and then
        # this returns false exactly as before.
        from app.services.cluster.remote_hub import note_demand
        return note_demand(cam_id)

    def get_status(self) -> dict[int, dict]:
        now = time.monotonic()
        out: dict[int, dict] = {}
        for cam_id, worker in self._workers.items():
            info = self.hub.get(cam_id).status()
            info.update(
                running=self.is_running(cam_id),
                pid=worker.pid if worker.proc and worker.proc.returncode is None else None,
                uptime=None if worker.started_at is None else round(now - worker.started_at, 1),
                next_planned_restart_in=(
                    None if worker.next_planned_at is None else round(worker.next_planned_at - now, 1)
                ),
            )
            out[cam_id] = info
        return out

    # ------------------------------------------------------------------ worker
    def _args(self, url: str) -> list[str]:
        timeout_us = str(int(self.config.rtsp_timeout_seconds * 1_000_000))
        return [
            settings.FFMPEG_BINARY,
            "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-timeout", timeout_us,  # RTSP socket I/O timeout, microseconds (ffmpeg 8 has no -rw_timeout for RTSP)
            "-fflags", "nobuffer",
            "-analyzeduration", "1000000",
            "-probesize", "1000000",
            "-i", url,
            "-map", "0:v:0", "-map", "0:a:0?",
            # video is copied as is; audio (often G.711, which mp4 cannot carry) always -> AAC
            "-c:v", "copy",
            "-c:a", "aac", "-ar", "48000", "-ac", "1", "-b:a", "64k",
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "pipe:1",
        ]

    async def _run(self, worker: _Worker) -> None:
        cam_id = worker.target.cam_id
        hub = self.hub.get(cam_id)
        backoff = self.config.backoff_initial_seconds
        try:
            while True:
                started = time.monotonic()
                reason = await self._run_once(worker)
                ran_stable = (
                    hub.last_data_at is not None
                    and hub.last_data_at >= started
                    and time.monotonic() - started >= self.config.stable_seconds
                )
                if reason in ("planned", "requested"):
                    delay = _RESPAWN_PAUSE
                else:
                    if ran_stable:
                        backoff = self.config.backoff_initial_seconds
                    delay = backoff
                    backoff = min(backoff * 2, self.config.backoff_max_seconds)
                    logger.warning(
                        "ingest: cam %s %s: %s; restart in %.1fs",
                        cam_id, reason, hub.last_error or "no details", delay,
                    )
                hub.restarts += 1
                worker.backoff = delay
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ingest: worker for cam %s crashed", cam_id)
            hub.last_error = "supervisor error"
            hub.set_state(STATE_OFFLINE)

    async def _run_once(self, worker: _Worker) -> str:
        cam_id = worker.target.cam_id
        hub = self.hub.get(cam_id)
        registry = self._ensure_registry()
        worker.restart_event.clear()
        worker.last_stderr = ""
        hub.set_state(STATE_CONNECTING)
        binary = settings.FFMPEG_BINARY
        tag = ownership.make_tag(registry.instance_id, cam_id)
        env = {**os.environ, ownership.ENV_TAG: tag}
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._args(worker.target.url),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            hub.last_error = mask_credentials(f"cannot start ffmpeg: {exc}")
            hub.set_state(STATE_OFFLINE)
            return "spawn_failed"

        started = time.monotonic()
        worker.proc, worker.pid, worker.tag, worker.binary = proc, proc.pid, tag, binary
        worker.start_time = ownership.read_start_time(proc.pid)
        worker.started_at = started
        if worker.start_time is not None:
            registry.add(cam_id, proc.pid, worker.start_time)
        tasks: list[asyncio.Task] = []
        try:
            reader = asyncio.create_task(self._read_stdout(hub, proc))
            stderr_task = asyncio.create_task(self._read_stderr(worker, proc))
            monitor = asyncio.create_task(self._monitor(worker, started))
            tasks = [reader, stderr_task, monitor]
            done, _ = await asyncio.wait({reader, monitor}, return_when=asyncio.FIRST_COMPLETED)
            if monitor in done and not monitor.cancelled():
                return monitor.result()
            # stdout closed: the process ended (or closed its output)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            detail = worker.last_stderr or "no stderr"
            hub.last_error = f"ffmpeg exited (code {proc.returncode}): {detail}"
            return "exited"
        finally:
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=2.0)
            try:
                await self._terminate(worker, proc)
            finally:
                registry.remove(cam_id, proc.pid)
                worker.proc = None
                worker.pid = None
                worker.started_at = None
                worker.next_planned_at = None
                hub.reset_stream("restart", STATE_OFFLINE)

    async def _terminate(self, worker: _Worker, proc: asyncio.subprocess.Process) -> None:
        outcome = "gone"
        if worker.pid is not None:
            outcome = await ownership.kill_tree_safely(
                worker.pid,
                start_time=worker.start_time,
                binary=worker.binary,
                tag=worker.tag,
                term_timeout=self.config.term_timeout_seconds,
                kill_timeout=self.config.kill_timeout_seconds,
            )
        if outcome == "not_ours" and proc.returncode is None:
            # /proc unreadable or similar: it is still our own unreaped child
            logger.warning("ingest: cannot verify ffmpeg of cam %s via /proc, killing via handle",
                           worker.target.cam_id)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2.0)

    async def _read_stdout(self, hub, proc: asyncio.subprocess.Process) -> None:
        while True:
            chunk = await proc.stdout.read(_READ_CHUNK_SIZE)
            if not chunk:
                return
            try:
                hub.feed(chunk)
            except Exception:
                logger.exception("ingest: hub.feed failed for cam %s", hub.cam_id)

    async def _read_stderr(self, worker: _Worker, proc: asyncio.subprocess.Process) -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = mask_credentials(line.decode(errors="replace").strip())
            if text:
                worker.last_stderr = text[:500]

    async def _monitor(self, worker: _Worker, started: float) -> str:
        """Returns why the current process must be replaced: stall / requested / planned."""
        cfg = self.config
        hub = self.hub.get(worker.target.cam_id)
        low, high = sorted((cfg.restart_min_minutes, cfg.restart_max_minutes))
        planned_at = started + random.uniform(low, high) * 60.0
        worker.next_planned_at = planned_at
        while True:
            await asyncio.sleep(cfg.monitor_tick_seconds)
            now = time.monotonic()
            last = max(hub.last_data_at or 0.0, started)
            if now - last > cfg.stall_seconds:
                hub.last_error = f"no data for {cfg.stall_seconds:g}s"
                return "stalled"
            if worker.restart_event.is_set():
                return "requested"
            if now >= planned_at:
                if hub.subscriber_count == 0:
                    return "planned"
                planned_at = now + cfg.restart_recheck_seconds
                worker.next_planned_at = planned_at
