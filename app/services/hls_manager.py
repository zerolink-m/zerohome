from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import time
from pathlib import Path

from app.core.config import settings
from app.models.cams import Cam
from app.services.ingest import ownership
from app.services.rtsp_url import build_rtsp_url as _build_rtsp_url
from app.services.rtsp_url import mask_credentials

logger = logging.getLogger(__name__)

_STDIN_DRAIN_TIMEOUT_SECONDS = 10.0  # transcoder not reading its stdin this long -> give up
_TERM_TIMEOUT_SECONDS = 2.0  # ffmpeg: SIGTERM, then SIGKILL after this
_KILL_TIMEOUT_SECONDS = 2.0
_STOP_ALL_TIMEOUT_SECONDS = 6.0
_MONITOR_TICK_SECONDS = 1.0  # watchdog / process-exit check period
_READY_POLL_SECONDS = 0.05

_MEDIA_SEQUENCE_RE = re.compile(r"^#EXT-X-MEDIA-SEQUENCE:(\d+)\s*$", re.M)


class HLSStreamError(Exception):
    pass


class _HLSSession:
    def __init__(self, cam_id: int, rtsp_url: str):
        self.cam_id = cam_id
        self.rtsp_url = rtsp_url
        self.process: asyncio.subprocess.Process | None = None
        self.active_clients: int = 0
        self.idle_task: asyncio.Task | None = None
        self.last_access_time: float = time.time()
        self.temp_dir = Path(settings.HLS_TEMP_DIR) / str(cam_id)
        self.playlist_path = self.temp_dir / "playlist.m3u8"
        self.active_marker = self.temp_dir / ".active"
        # --- source: "direct" (own RTSP ffmpeg) | "hub" (fMP4 from the ingest hub on stdin) ---
        self.source: str = "direct"
        self.sub = None  # hub Subscription feeding ffmpeg's stdin
        self.feeder_task: asyncio.Task | None = None
        self.stderr_task: asyncio.Task | None = None
        self.monitor_task: asyncio.Task | None = None
        self.last_stderr_line: str = ""
        self.end_reason: str | None = None
        self.pid: int | None = None
        self.start_time: int | None = None
        self.tag: str = ""
        self.binary: str = ""
        self.started_at: float = time.monotonic()
        self.stopping: bool = False
        self.stopped: asyncio.Event = asyncio.Event()
        # Playlist continuity across ffmpeg restarts (see _continuity): every new ffmpeg
        # restarts its timestamps, so the sequence keeps counting and the epoch (number
        # of restarts) is announced to players as an EXT-X-DISCONTINUITY.
        self.start_seq, self.epoch = _continuity.get(cam_id, (0, 0))
        self.segment_stamp: int = int(time.time() * 1000)

    @property
    def alive(self) -> bool:
        return (
            not self.stopping
            and self.process is not None
            and self.process.returncode is None
        )

    # --- args ---------------------------------------------------------------------

    def _output_args(self) -> list[str]:
        # Unique segment names per ffmpeg run (sequence number + start stamp), so a
        # long-cached segment of an earlier run can never be mistaken for a new one.
        # (The former `-strftime 1` + `seg_%d_%%t` expanded %d to the day of month: every
        # segment got the same name.) No append_list: the directory is always emptied
        # before a start and append_list would add a bogus leading discontinuity.
        return [
            "-f", "hls",
            "-start_number", str(self.start_seq),
            "-hls_time", str(settings.HLS_SEGMENT_DURATION),
            "-hls_list_size", str(settings.HLS_PLAYLIST_SIZE),
            "-hls_flags", "program_date_time",
            "-hls_segment_filename", str(self.temp_dir / f"seg_%d_{self.segment_stamp}.ts"),
            str(self.playlist_path),
        ]

    def _direct_args(self) -> list[str]:
        return [
            settings.FFMPEG_BINARY,
            "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-analyzeduration", "10000000",
            "-probesize", "10000000",
            "-i", self.rtsp_url,
            "-c:v", "libx264",
            "-profile:v", "main",
            "-level", "4.0",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-g", "30",
            "-keyint_min", "30",
            "-c:a", "aac",
            "-ar", "48000",
            "-ac", "1",
            "-b:a", "64k",
            *self._output_args(),
        ]

    def _hub_args(self) -> list[str]:
        # Same video output as the direct path, input = the hub's fMP4 on stdin. No `-re`
        # (the replayed GOP must be processed faster than real time: the first segment is
        # ready ~0.3 s after start) and no `-fflags nobuffer` (with ffmpeg 8.0.1 it makes
        # ffmpeg drop the packets consumed while probing). The hub's audio is already
        # AAC 48k mono 64k, so it is copied. `-hls_init_time` was measured: no effect
        # (the first cut needs a keyframe anyway), so it is not used.
        return [
            settings.FFMPEG_BINARY,
            "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-analyzeduration", "1000000",
            "-probesize", "1000000",
            "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264",
            "-profile:v", "main",
            "-level", "4.0",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-g", "30",
            "-keyint_min", "30",
            "-c:a", "copy",
            *self._output_args(),
        ]

    # --- lifecycle ----------------------------------------------------------------

    async def start(self, hub=None) -> None:
        """Starts ffmpeg (from the hub when `hub` is given, else straight from RTSP) and
        waits until the playlist lists a finished segment."""
        self._reset_dir()
        self.source = "hub" if hub is not None else "direct"
        registry = _ensure_registry()
        self.tag = ownership.make_tag(registry.instance_id, self.cam_id)
        self.binary = settings.FFMPEG_BINARY
        env = {**os.environ, ownership.ENV_TAG: self.tag}
        args = self._hub_args() if hub is not None else self._direct_args()
        sub = None
        if hub is not None:
            from app.services.ingest import get_hub  # late: avoids import cycles

            sub = get_hub().subscribe(self.cam_id)  # subscribe first: the GOP is replayed from now on
        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if hub is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            if sub is not None:
                sub.close()
            self._cleanup_files()
            raise HLSStreamError(f"ffmpeg не найден: {exc}") from exc

        self.sub = sub
        self.pid = self.process.pid
        self.started_at = time.monotonic()
        self.start_time = ownership.read_start_time(self.pid)
        if self.start_time is not None:
            registry.add(self.cam_id, self.pid, self.start_time)
        self.stderr_task = asyncio.create_task(self._stderr_loop())
        if sub is not None:
            self.feeder_task = asyncio.create_task(self._feed_loop())

        deadline = time.monotonic() + float(settings.HLS_START_TIMEOUT_SECONDS)
        while time.monotonic() < deadline:
            if self._first_segment_ready():
                self.monitor_task = asyncio.create_task(self._monitor_loop())
                return
            if self.process.returncode is not None or self.stopping:
                break
            await asyncio.sleep(_READY_POLL_SECONDS)

        detail = self.end_reason or self.last_stderr_line
        exited = self.process.returncode is not None
        await self.stop(self.end_reason or ("ffmpeg exited" if exited else "no first segment in time"))
        suffix = f": {detail}" if detail else ""
        if exited:
            raise HLSStreamError(f"ffmpeg завершился до создания HLS playlist{suffix}")
        raise HLSStreamError(f"Не удалось создать HLS playlist (таймаут){suffix}")

    def _first_segment_ready(self) -> bool:
        try:
            text = self.playlist_path.read_text()
        except OSError:
            return False
        for line in text.splitlines():
            entry = line.strip()
            if entry and not entry.startswith("#") and (self.temp_dir / entry).is_file():
                return True
        return False

    async def _stderr_loop(self) -> None:
        stderr = self.process.stderr if self.process is not None else None
        if stderr is None:
            return
        while True:
            line = await stderr.readline()
            if not line:
                return
            text = mask_credentials(line.decode(errors="replace").strip())
            if text:
                self.last_stderr_line = text[:500]

    async def _feed_loop(self) -> None:
        """Hub subscription -> ffmpeg stdin. A feed that ends by itself (ingest restart /
        overflow / stop: new init segment and timestamps follow) ends the session; the next
        playlist request starts a fresh one."""
        stdin = self.process.stdin
        sub = self.sub
        reason = None
        try:
            async for chunk in sub:
                stdin.write(chunk)
                await asyncio.wait_for(stdin.drain(), timeout=_STDIN_DRAIN_TIMEOUT_SECONDS)
            reason = f"hub feed ended ({sub.close_reason or 'closed'})"
        except (BrokenPipeError, ConnectionResetError):
            reason = "ffmpeg stdin closed"
        except asyncio.TimeoutError:
            reason = "ffmpeg not reading input"
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("hls: feeder for cam %s failed", self.cam_id)
            reason = "feeder error"
        await _end_by_itself(self, reason)

    async def _monitor_loop(self) -> None:
        """Watchdog: ffmpeg exited, or no new segment for HLS_STALL_SECONDS."""
        stall = float(settings.HLS_STALL_SECONDS)
        while True:
            await asyncio.sleep(_MONITOR_TICK_SECONDS)
            if self.stopping:
                return
            if self.process is None or self.process.returncode is not None:
                await _end_by_itself(self, f"ffmpeg exited ({self.process.returncode if self.process else '?'})")
                return
            try:
                last = self.playlist_path.stat().st_mtime
                idle = max(0.0, time.time() - last)
            except OSError:
                idle = time.monotonic() - self.started_at
            if idle > stall:
                logger.warning("hls: cam %s no new segment for %gs, killing ffmpeg", self.cam_id, stall)
                await _end_by_itself(self, "ffmpeg stalled")
                return

    async def stop(self, reason: str = "stopped") -> None:
        """Stops ffmpeg (ownership-checked kill of its group) and removes the files.
        Idempotent; later callers wait for the first to finish."""
        if self.stopping:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stopped.wait(), timeout=_TERM_TIMEOUT_SECONDS + _KILL_TIMEOUT_SECONDS + 3)
            return
        self.stopping = True
        self.end_reason = self.end_reason or reason
        try:
            current = asyncio.current_task()
            for task in (self.feeder_task, self.monitor_task):
                if task is not None and task is not current:
                    task.cancel()
            if self.idle_task is not None and self.idle_task is not current:
                self.idle_task.cancel()
            if self.sub is not None:
                self.sub.close()
            await self._kill_process()
            if self.stderr_task is not None:
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    await asyncio.wait_for(self.stderr_task, timeout=1.0)
        finally:
            _registry_remove(self)
            self._remember_continuity()
            self._cleanup_files()
            self.stopped.set()

    async def _kill_process(self) -> None:
        proc = self.process
        if proc is None:
            return
        if proc.returncode is None and self.pid is not None:
            outcome = await ownership.kill_tree_safely(
                self.pid,
                start_time=self.start_time,
                binary=self.binary,
                tag=self.tag,
                term_timeout=_TERM_TIMEOUT_SECONDS,
                kill_timeout=_KILL_TIMEOUT_SECONDS,
            )
            if outcome == "not_ours" and proc.returncode is None:
                # /proc unreadable or similar: still our own unreaped child
                logger.warning("hls: cannot verify ffmpeg of cam %s via /proc, killing via handle", self.cam_id)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2.0)

    def force_kill(self) -> None:
        """Synchronous last resort (shutdown budget exhausted); still ownership-checked."""
        if self.pid is not None:
            ownership.kill_now(self.pid, start_time=self.start_time, binary=self.binary, tag=self.tag)
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()

    # --- files ----------------------------------------------------------------------

    def _reset_dir(self) -> None:
        """Empties the camera's directory (stale playlist/segments of an earlier run
        would otherwise be served as if they were new)."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.active_marker.touch()

    def _cleanup_files(self) -> None:
        """Remove all HLS files for this camera"""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _remember_continuity(self) -> None:
        next_seq = self.start_seq + 1
        try:
            text = self.playlist_path.read_text()
            m = _MEDIA_SEQUENCE_RE.search(text)
            count = sum(1 for ln in text.splitlines() if ln.strip().endswith(".ts"))
            if m:
                next_seq = int(m.group(1)) + count
        except OSError:
            try:
                next_seq = self.start_seq + sum(1 for f in self.temp_dir.iterdir() if f.name.endswith(".ts")) + 1
            except OSError:
                pass
        _continuity[self.cam_id] = (max(next_seq, self.start_seq + 1), self.epoch + 1)

    def playlist_text(self) -> str:
        """The playlist as ffmpeg wrote it ("" when missing). After an ffmpeg restart the
        segments carry on numbering but timestamps start over, so the first segment of
        the new run is preceded by EXT-X-DISCONTINUITY and EXT-X-DISCONTINUITY-SEQUENCE
        counts the restarts (ffmpeg itself neither adds the tag for a fresh run nor keeps
        the sequence when the tagged segment slides out of the window)."""
        try:
            text = self.playlist_path.read_text()
        except OSError:
            return ""
        if self.epoch <= 0:
            return text
        m = _MEDIA_SEQUENCE_RE.search(text)
        if m is None:
            return text
        first_of_run = int(m.group(1)) == self.start_seq
        disc_seq = self.epoch - 1 if first_of_run else self.epoch
        out = []
        for line in text.splitlines():
            if line.startswith("#EXTINF:") and first_of_run:
                out.append("#EXT-X-DISCONTINUITY")
                first_of_run = False
            out.append(line)
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:") and disc_seq > 0:
                out.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{disc_seq}")
        return "\n".join(out) + "\n"

    def touch(self) -> None:
        """Update last access time"""
        self.last_access_time = time.time()


_sessions: dict[int, _HLSSession] = {}
_cam_locks: dict[int, asyncio.Lock] = {}
_guard: asyncio.Lock = asyncio.Lock()
_cleanup_task: asyncio.Task | None = None
# cam_id -> (first segment number, number of earlier ffmpeg runs) for the next run
_continuity: dict[int, tuple[int, int]] = {}


def _cam_lock(cam_id: int) -> asyncio.Lock:
    lock = _cam_locks.get(cam_id)
    if lock is None:
        lock = asyncio.Lock()
        _cam_locks[cam_id] = lock
    return lock


# --- process registry (own file next to the ingest/transcoder ones) ---------------------

_registry: ownership.Registry | None = None


def _registry_path() -> Path:
    return Path(settings.ingest_registry_path).with_name("hls_registry.json")


def _ensure_registry() -> ownership.Registry:
    global _registry
    path = _registry_path()
    if _registry is None or _registry.path != path:
        _registry = ownership.Registry(path)
        _registry.save()
    return _registry


def _registry_remove(session: _HLSSession) -> None:
    if _registry is not None and session.pid is not None:
        _registry.remove(session.cam_id, session.pid)


async def startup() -> None:
    """Reaps HLS ffmpegs left by a previous run (verified by registry + start time + env
    tag + binary, never by name) and starts a fresh registry. Call once at app startup,
    before any viewer. Does nothing when the previous run left no registry."""
    global _registry
    try:
        report = await ownership.reap_orphans(_registry_path(), binary=settings.FFMPEG_BINARY)
        if any(report.values()):
            logger.info("hls: orphan sweep: %s", report)
    except Exception:
        logger.exception("hls: orphan sweep failed")
    _registry = None
    if settings.HLS_ENABLED:
        _ensure_registry()


async def _hub_ready(cam: Cam):
    """The cam's CamHub when ingest is on, running for this camera and online (waiting
    up to INGEST_HUB_WAIT_SECONDS for it), else None."""
    if not settings.INGEST_ENABLED:
        return None
    from app.services.ingest import get_hub, get_supervisor  # late: avoids import cycles

    try:
        if not get_supervisor().is_running(cam.id):
            return None
        hub = get_hub().get(cam.id)
        if not (hub.online.is_set() and hub.init_segment is not None):
            await asyncio.wait_for(hub.online.wait(), timeout=settings.INGEST_HUB_WAIT_SECONDS)
        if hub.init_segment is None:
            return None
        return hub
    except asyncio.TimeoutError:
        return None
    except Exception:
        logger.exception("hls: hub check failed for cam %s", cam.id)
        return None


async def _end_by_itself(session: _HLSSession, reason: str) -> None:
    """ffmpeg died / stalled / lost its feed: drop the session and its files; the next
    playlist request starts a new one."""
    async with _guard:
        if _sessions.get(session.cam_id) is session:
            _sessions.pop(session.cam_id, None)
    logger.info("hls: cam %s session ended (%s)", session.cam_id, reason)
    await session.stop(reason)


async def get_or_create_session(cam: Cam) -> _HLSSession:
    """Get existing HLS session or create a new one"""
    started = time.monotonic()
    async with _cam_lock(cam.id):
        async with _guard:
            session = _sessions.get(cam.id)
            if session is not None:
                # Check if process is still alive
                if session.alive:
                    session.touch()
                    if session.idle_task is not None:
                        session.idle_task.cancel()
                        session.idle_task = None
                    return session
                # Process died, clean up
                _sessions.pop(cam.id, None)
        if session is not None:
            await session.stop("dead")

        # Create new session: from the hub when it can serve this camera, else direct RTSP
        session = None
        hub = await _hub_ready(cam)
        if hub is not None:
            candidate = _HLSSession(cam.id, "")
            try:
                await candidate.start(hub)
                session = candidate
            except HLSStreamError as exc:
                logger.warning("hls: cam %s hub source failed (%s), falling back to direct", cam.id, exc)
        elif settings.INGEST_ENABLED:
            logger.info("hls: cam %s hub not available, using direct RTSP", cam.id)
        if session is None:
            session = _HLSSession(cam.id, _build_rtsp_url(cam))
            await session.start()

        async with _guard:
            _sessions[cam.id] = session
        if session.stopping:  # died between "ready" and registration
            async with _guard:
                _sessions.pop(cam.id, None)
            raise HLSStreamError("HLS ffmpeg завершился сразу после старта")
        logger.info("hls: cam %s source=%s first_playlist_ms=%d",
                    cam.id, session.source, (time.monotonic() - started) * 1000)
        return session


def schedule_idle_stop(cam_id: int) -> None:
    """Schedule session stop after idle timeout"""

    async def _do_schedule():
        async with _guard:
            session = _sessions.get(cam_id)
            if session is None:
                return

            if session.idle_task is not None:
                session.idle_task.cancel()

            session.idle_task = asyncio.create_task(_idle_stop(cam_id, session))

    asyncio.create_task(_do_schedule())


async def _idle_stop(cam_id: int, session: _HLSSession) -> None:
    """Stop session after idle timeout. HLS has no connection to watch, so "viewer gone"
    means no request for HLS_IDLE_TIMEOUT_SECONDS (a player polls the playlist every
    segment, ~2 s); that already is the linger, so it is used for the hub source too
    (INGEST_IDLE_TRANSCODE_LINGER_SECONDS, 45 s, is for /stream where the viewer is a
    long-lived connection and re-attaching must be instant). Ingest keeps running."""
    await asyncio.sleep(settings.HLS_IDLE_TIMEOUT_SECONDS)

    async with _guard:
        # Check if still the same session and no recent activity
        current = _sessions.get(cam_id)
        if current is not session:
            return

        time_since_access = time.time() - session.last_access_time
        if time_since_access < settings.HLS_IDLE_TIMEOUT_SECONDS:
            # Activity detected, reschedule
            session.idle_task = asyncio.create_task(_idle_stop(cam_id, session))
            return

        _sessions.pop(cam_id, None)

    if session.source == "hub":
        logger.info("hls: cam %s idle, stopping transcoder (ingest keeps running)", cam_id)
    await session.stop("idle")


async def stop(cam_id: int) -> None:
    """Force stop HLS session for a camera"""
    async with _guard:
        session = _sessions.pop(cam_id, None)

    if session is None:
        return

    if session.idle_task is not None:
        session.idle_task.cancel()

    await session.stop("stopped")


async def stop_all(timeout: float = _STOP_ALL_TIMEOUT_SECONDS) -> None:
    """App shutdown: ends every session within `timeout` overall (then SIGKILL)."""
    async with _guard:
        sessions = list(_sessions.values())
        _sessions.clear()
    if not sessions:
        return
    tasks = [asyncio.create_task(s.stop("shutdown")) for s in sessions]
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        logger.warning("hls: shutdown budget exceeded, force-killing the rest")
        for session in sessions:
            session.force_kill()
        for task in pending:
            task.cancel()
        await asyncio.wait(pending, timeout=1.0)


async def _cleanup_loop() -> None:
    """Background task to cleanup old HLS files"""
    while True:
        try:
            await asyncio.sleep(settings.HLS_CLEANUP_INTERVAL)
            await _cleanup_old_files()
        except asyncio.CancelledError:
            break
        except Exception:
            # Log error but continue
            pass


async def _cleanup_old_files() -> None:
    """Remove old/orphaned HLS files"""
    temp_dir = Path(settings.HLS_TEMP_DIR)
    if not temp_dir.exists():
        return

    now = time.time()

    for cam_dir in temp_dir.iterdir():
        if not cam_dir.is_dir():
            continue

        active_marker = cam_dir / ".active"

        # If no active marker, check if directory is old enough to delete
        if not active_marker.exists():
            try:
                dir_mtime = cam_dir.stat().st_mtime
                if now - dir_mtime > settings.HLS_SEGMENT_MAX_AGE:
                    shutil.rmtree(cam_dir, ignore_errors=True)
            except Exception:
                pass
            continue

        # Clean up old segments in active directories
        try:
            for file in cam_dir.iterdir():
                if file.name.endswith(".ts"):
                    file_age = now - file.stat().st_mtime
                    if file_age > settings.HLS_SEGMENT_MAX_AGE:
                        file.unlink(missing_ok=True)
        except Exception:
            pass


def start_cleanup_task() -> None:
    """Start background cleanup task"""
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_loop())


async def stop_cleanup_task() -> None:
    """Stop background cleanup task"""
    global _cleanup_task
    if _cleanup_task is not None and not _cleanup_task.done():
        _cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _cleanup_task
