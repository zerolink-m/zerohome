from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

from app.core.config import settings
from app.models.cams import Cam
from app.services.fmp4 import Fmp4Splitter
from app.services.ingest import ownership
from app.services.rtsp_url import build_rtsp_url as _build_rtsp_url
from app.services.rtsp_url import mask_credentials

logger = logging.getLogger(__name__)


_READ_CHUNK_SIZE = 64 * 1024
_FIRST_CHUNK_TIMEOUT_SECONDS = 10.0
_STDIN_DRAIN_TIMEOUT_SECONDS = 10.0  # transcoder not reading its stdin this long -> give up
_TERM_TIMEOUT_SECONDS = 2.0  # transcoder: SIGTERM, then SIGKILL after this
_KILL_TIMEOUT_SECONDS = 2.0
_STOP_ALL_TIMEOUT_SECONDS = 6.0


class StreamStartError(Exception):
    pass


class _StreamSession:
    def __init__(self, process: asyncio.subprocess.Process):
        self.process: asyncio.subprocess.Process = process
        self.subscribers: set[asyncio.Queue] = set()
        self.stderr_task: asyncio.Task | None = None
        self.reader_task: asyncio.Task | None = None
        self.idle_task: asyncio.Task | None = None
        self.init_ready: asyncio.Event = asyncio.Event()
        self.init_segment: bytes | None = None  # ftyp + moov, replayed to every subscriber
        self._splitter: Fmp4Splitter = Fmp4Splitter()  # frames raw pipe reads into whole segments
        self.init_done: bool = False
        self.last_stderr_line: str = ""
        self.bootstrapped: bool = False
        # --- hub source (on-demand transcoder fed from the ingest hub) ---
        self.source: str = "direct"  # "direct" (own RTSP ffmpeg) | "hub" (transcoder)
        self.idle_timeout: float = float(settings.STREAM_IDLE_TIMEOUT_SECONDS)
        self.sub = None  # hub Subscription feeding the transcoder's stdin
        self.feeder_task: asyncio.Task | None = None
        self.end_reason: str | None = None
        self.pid: int | None = None
        self.start_time: int | None = None
        self.tag: str = ""
        self.binary: str = ""
        self.cam_id: int = 0

    def broadcast(self, chunk: bytes) -> None:
        was_done = self._splitter.init_done
        segments = self._splitter.feed(chunk)
        if self._splitter.init_segment is not None:
            self.init_segment = self._splitter.init_segment
        if self._splitter.init_done and not was_done:
            self.init_done = True
            self.init_ready.set()
        for segment in segments:
            self._publish(segment)

    def _publish(self, chunk: bytes) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass

    def close_all(self) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self.subscribers.clear()


_sessions: dict[int, _StreamSession] = {}
_cam_locks: dict[int, asyncio.Lock] = {}
_guard: asyncio.Lock = asyncio.Lock()


def _cam_lock(cam_id: int) -> asyncio.Lock:
    lock = _cam_locks.get(cam_id)
    if lock is None:
        lock = asyncio.Lock()
        _cam_locks[cam_id] = lock
    return lock


async def _stderr_loop(session: _StreamSession) -> None:
    stderr = session.process.stderr
    if stderr is None:
        return
    while True:
        line = await stderr.readline()
        if not line:
            return
        text = mask_credentials(line.decode(errors="replace").strip())
        if text:
            session.last_stderr_line = text[:500]


async def _reader_loop(cam_id: int, session: _StreamSession) -> None:
    proc = session.process
    stall = settings.INGEST_TRANSCODE_STALL_SECONDS if session.source == "hub" else None
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(_READ_CHUNK_SIZE), timeout=stall)
            except asyncio.TimeoutError:
                session.end_reason = session.end_reason or "transcoder stalled"
                logger.warning("stream: cam %s transcoder produced no output for %gs, killing", cam_id, stall)
                break
            if not chunk:
                break
            session.broadcast(chunk)
    finally:
        session.init_ready.set()
        session.close_all()
        async with _guard:
            if _sessions.get(cam_id) is session:
                _sessions.pop(cam_id, None)
        if session.idle_task is not None and session.idle_task is not asyncio.current_task():
            session.idle_task.cancel()
        await _kill_session(session)


async def _kill_session(session: _StreamSession) -> None:
    """Ends the process of a session (idempotent). Direct sessions: plain SIGTERM as
    before. Hub sessions: the transcoder is ours by registry+start time+env tag, so it
    goes through ownership.kill_tree_safely (SIGTERM, wait, SIGKILL of its group)."""
    proc = session.process
    if session.source != "hub":
        with contextlib.suppress(ProcessLookupError):
            if proc.returncode is None:
                proc.terminate()
        return
    try:
        if session.feeder_task is not None and session.feeder_task is not asyncio.current_task():
            session.feeder_task.cancel()
        if session.sub is not None:
            session.sub.close()
        if proc.returncode is None and session.pid is not None:
            outcome = await ownership.kill_tree_safely(
                session.pid,
                start_time=session.start_time,
                binary=session.binary,
                tag=session.tag,
                term_timeout=_TERM_TIMEOUT_SECONDS,
                kill_timeout=_KILL_TIMEOUT_SECONDS,
            )
            if outcome == "not_ours" and proc.returncode is None:
                # /proc unreadable or similar: still our own unreaped child
                logger.warning("stream: cannot verify transcoder of cam %s via /proc, killing via handle",
                               session.cam_id)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
    finally:
        _registry_remove(session)


def _force_kill_session(session: _StreamSession) -> None:
    """Synchronous last resort (shutdown budget exhausted); still ownership-checked."""
    if session.pid is not None:
        ownership.kill_now(session.pid, start_time=session.start_time, binary=session.binary, tag=session.tag)
    if session.process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            session.process.kill()


# --- transcoder process registry (separate file from the ingest registry) -------------

_registry: ownership.Registry | None = None


def _registry_path() -> Path:
    return Path(settings.ingest_registry_path).with_name("transcode_registry.json")


def _ensure_registry() -> ownership.Registry:
    global _registry
    path = _registry_path()
    if _registry is None or _registry.path != path:
        _registry = ownership.Registry(path)
        _registry.save()
    return _registry


def _registry_remove(session: _StreamSession) -> None:
    if _registry is not None and session.pid is not None:
        _registry.remove(session.cam_id, session.pid)


async def startup() -> None:
    """Reaps transcoders left by a previous run (verified by registry + start time +
    env tag + binary, never by name) and opens this run's registry. No-op unless
    INGEST_ENABLED. Call once at app startup, before any viewer."""
    global _registry
    if not settings.INGEST_ENABLED:
        return
    try:
        report = await ownership.reap_orphans(_registry_path(), binary=settings.FFMPEG_BINARY)
        logger.info("stream: transcoder orphan sweep: %s", report)
    except Exception:
        logger.exception("stream: transcoder orphan sweep failed")
    _registry = None
    _ensure_registry()


async def _spawn(cam: Cam) -> _StreamSession:
    args = [
        settings.FFMPEG_BINARY,
        "-rtsp_transport", "tcp",
        "-analyzeduration", "10000000",
        "-probesize", "10000000",
        "-i", _build_rtsp_url(cam),
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
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "pipe:1",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise StreamStartError(f"ffmpeg не найден: {exc}") from exc

    session = _StreamSession(process)
    session.cam_id = cam.id
    reader_task = asyncio.create_task(_reader_loop(cam.id, session))
    session.reader_task = reader_task
    session.stderr_task = asyncio.create_task(_stderr_loop(session))

    try:
        await asyncio.wait_for(
            session.init_ready.wait(),
            timeout=_FIRST_CHUNK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        detail = f": {session.last_stderr_line}" if session.last_stderr_line else ""
        raise StreamStartError(f"Таймаут подключения к камере (RTSP){detail}") from exc

    if session.init_segment is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        detail = f": {session.last_stderr_line}" if session.last_stderr_line else ""
        raise StreamStartError(f"Не удалось подключиться к камере (RTSP){detail}")

    return session


class _HubUnavailable(Exception):
    """The hub cannot serve this camera right now: use the direct RTSP path."""


def _transcoder_args() -> list[str]:
    # Same output as the direct path (libx264 main/4.0, -g 30, fMP4 on stdout), but the
    # input is the hub's fMP4 on stdin. No `-re`: the replayed GOP must be processed
    # faster than real time to catch up with the live edge. Audio is already AAC 48k
    # mono 64k in the hub, so it is copied.
    # No `-fflags nobuffer`: measured with ffmpeg 8.0.1 it makes ffmpeg drop the packets
    # it consumed while probing, i.e. the replayed GOP is lost (output starts at the next
    # keyframe, 2-4 s later) and the HEVC decoder logs RPS errors.
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
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "pipe:1",
    ]


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
        logger.exception("stream: hub check failed for cam %s", cam.id)
        return None


async def _feed_loop(session: _StreamSession) -> None:
    """Hub subscription -> transcoder stdin. When the subscription ends on its own
    (ingest restart / overflow / stop) the transcoder is killed so viewers are cut off."""
    stdin = session.process.stdin
    sub = session.sub
    reason = None
    try:
        async for chunk in sub:
            stdin.write(chunk)
            await asyncio.wait_for(stdin.drain(), timeout=_STDIN_DRAIN_TIMEOUT_SECONDS)
        reason = sub.close_reason or "hub closed"
    except (BrokenPipeError, ConnectionResetError):
        reason = "transcoder stdin closed"
    except asyncio.TimeoutError:
        reason = "transcoder not reading input"
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("stream: feeder for cam %s failed", session.cam_id)
        reason = "feeder error"
    # Only reached when the feed ended by itself (cancellation re-raised above).
    session.end_reason = session.end_reason or reason
    logger.info("stream: cam %s hub feed ended (%s), closing viewers", session.cam_id, reason)
    sub.close()
    with contextlib.suppress(Exception):
        stdin.close()
    await _kill_session(session)


async def _spawn_hub(cam: Cam, hub) -> _StreamSession:
    from app.services.ingest import get_hub  # late: avoids import cycles

    registry = _ensure_registry()
    tag = ownership.make_tag(registry.instance_id, cam.id)
    env = {**os.environ, ownership.ENV_TAG: tag}
    binary = settings.FFMPEG_BINARY
    sub = get_hub().subscribe(cam.id)  # subscribe first: the GOP is replayed from now on
    try:
        process = await asyncio.create_subprocess_exec(
            *_transcoder_args(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        sub.close()
        raise _HubUnavailable(f"cannot start transcoder: {exc}") from exc

    session = _StreamSession(process)
    session.source = "hub"
    session.idle_timeout = float(settings.INGEST_IDLE_TRANSCODE_LINGER_SECONDS)
    session.sub = sub
    session.cam_id = cam.id
    session.pid = process.pid
    session.tag = tag
    session.binary = binary
    session.start_time = ownership.read_start_time(process.pid)
    if session.start_time is not None:
        registry.add(cam.id, process.pid, session.start_time)
    session.reader_task = asyncio.create_task(_reader_loop(cam.id, session))
    session.stderr_task = asyncio.create_task(_stderr_loop(session))
    session.feeder_task = asyncio.create_task(_feed_loop(session))

    try:
        await asyncio.wait_for(session.init_ready.wait(), timeout=_FIRST_CHUNK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        session.end_reason = "no output from transcoder"
    if session.init_segment is None:
        detail = session.end_reason or session.last_stderr_line or "no init segment"
        await _kill_session(session)
        raise _HubUnavailable(f"transcoder failed: {detail}")
    return session


def _attach(session: _StreamSession) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    # ВАЖНО: всегда отправляем init_segment первым для каждого нового подключения
    if session.init_segment is not None:
        queue.put_nowait(session.init_segment)
    session.bootstrapped = True
    session.subscribers.add(queue)
    return queue


async def has_active_session(cam_id: int) -> bool:
    """Cheap pre-check: is there already a live ffmpeg session for this camera?
    Used to classify the point cost of /stream before charging for it."""
    async with _guard:
        session = _sessions.get(cam_id)
        return (
            session is not None
            and session.reader_task is not None
            and not session.reader_task.done()
        )


async def get_or_attach(cam: Cam) -> tuple[asyncio.Queue, _StreamSession]:
    started = time.monotonic()
    async with _cam_lock(cam.id):
        async with _guard:
            session = _sessions.get(cam.id)
            if (
                session is not None
                and session.reader_task is not None
                and not session.reader_task.done()
            ):
                if session.idle_task is not None:
                    session.idle_task.cancel()
                    session.idle_task = None
                queue = _attach(session)
                logger.info("stream: cam %s attach source=%s first_byte_ms=%d",
                            cam.id, session.source, (time.monotonic() - started) * 1000)
                return (queue, session)

        session = None
        hub = await _hub_ready(cam)
        if hub is not None:
            try:
                session = await _spawn_hub(cam, hub)
            except _HubUnavailable as exc:
                logger.warning("stream: cam %s hub source failed (%s), falling back to direct", cam.id, exc)
        elif settings.INGEST_ENABLED:
            logger.info("stream: cam %s hub not available, using direct RTSP", cam.id)
        if session is None:
            session = await _spawn(cam)
        async with _guard:
            _sessions[cam.id] = session
            queue = _attach(session)
        logger.info("stream: cam %s open source=%s first_byte_ms=%d",
                    cam.id, session.source, (time.monotonic() - started) * 1000)
        return (queue, session)


async def detach(cam_id: int, session: _StreamSession, queue: asyncio.Queue) -> None:
    async with _guard:
        session.subscribers.discard(queue)
        if not session.subscribers and _sessions.get(cam_id) is session:
            session.idle_task = asyncio.create_task(_idle_stop(cam_id, session))


async def _idle_stop(cam_id: int, session: _StreamSession) -> None:
    await asyncio.sleep(session.idle_timeout)
    async with _guard:
        if _sessions.get(cam_id) is not session or session.subscribers:
            return
        _sessions.pop(cam_id, None)
    if session.source == "hub":
        logger.info("stream: cam %s linger over, stopping transcoder (ingest keeps running)", cam_id)
    await _kill_session(session)


async def stop(cam_id: int) -> None:
    async with _guard:
        session = _sessions.pop(cam_id, None)
    if session is None:
        return
    if session.idle_task is not None:
        session.idle_task.cancel()
    await _kill_session(session)


async def stop_all(timeout: float = _STOP_ALL_TIMEOUT_SECONDS) -> None:
    """App shutdown: ends every session within `timeout` overall (then SIGKILL)."""
    async with _guard:
        sessions = list(_sessions.values())
        _sessions.clear()
    if not sessions:
        return
    for session in sessions:
        if session.idle_task is not None:
            session.idle_task.cancel()
    tasks = [asyncio.create_task(_kill_session(s)) for s in sessions]
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        logger.warning("stream: shutdown budget exceeded, force-killing the rest")
        for session in sessions:
            _force_kill_session(session)
        for task in pending:
            task.cancel()
        await asyncio.wait(pending, timeout=1.0)
    for session in sessions:
        session.close_all()


async def iter_queue(queue: asyncio.Queue):
    while True:
        chunk = await queue.get()
        if chunk is None:
            return
        yield chunk
