from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time

from app.core.config import settings
from app.models.cams import Cam
from app.services.ingest import consumer
from app.services.rtsp_url import build_rtsp_url, mask_credentials

logger = logging.getLogger(__name__)


class SnapshotError(Exception):
    pass


_PROBE_TIMEOUT_SECONDS = 10.0
_GRAB_TIMEOUT_SECONDS = 20.0
_SNAPSHOT_SCALE_FILTER = "scale='min(1280,iw)':-2"
_JPEG_QUALITY = "3"
DEFAULT_SEEK_SECONDS = 1.0

_validity_cache: dict[tuple[str, float, int], bool] = {}
_cache_locks: dict[str, asyncio.Lock] = {}

# --- hub source (stage 5): local ffmpeg over the hub's init + last GOP ---
# Own process pool/registry (snapshot_registry.json); see ingest/consumer.py.
_pool = consumer.OwnedProcessPool("snapshot", "snapshot_registry.json")
_hub_locks: dict[int, asyncio.Lock] = {}
_hub_last: dict[int, tuple[bytes, bytes]] = {}  # cam_id -> (digest of init+last fragment, jpeg)
_hub_sem: asyncio.Semaphore | None = None
_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"


def _cache_path(cam_id: int, recording_id: str) -> str:
    name, _ = os.path.splitext(recording_id)
    return os.path.join(settings.CACHE_DIR, str(cam_id), "snapshots", f"{name}.jpg")


def is_cached(cam_id: int, recording_id: str) -> bool:
    """Cheap pre-check: is the snapshot already on disk? Used to classify the point
cost of the snapshot endpoint before charging for it."""
    return os.path.isfile(_cache_path(cam_id, recording_id))


def _lock_for(cache_path: str) -> asyncio.Lock:
    lock = _cache_locks.get(cache_path)
    if lock is None:
        lock = asyncio.Lock()
        _cache_locks[cache_path] = lock
    return lock


async def is_valid_video(path: str) -> bool:
    stat_result = os.stat(path)
    cache_key = (path, stat_result.st_mtime, stat_result.st_size)
    cached = _validity_cache.get(cache_key)
    if cached is not None:
        return cached

    args = [
        settings.FFPROBE_BINARY,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise SnapshotError(f"ffprobe не найден: {exc}") from exc

    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=_PROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        _validity_cache[cache_key] = False
        return False

    valid = False
    if process.returncode == 0:
        try:
            valid = float(stdout.strip()) > 0
        except ValueError:
            valid = False
    _validity_cache[cache_key] = valid
    return valid


async def _grab(path: str, seek_seconds: float) -> bytes:
    args = [
        settings.FFMPEG_BINARY,
        "-ss", str(seek_seconds),
        "-i", path,
        "-frames:v", "1",
        "-vf", _SNAPSHOT_SCALE_FILTER,
        "-q:v", _JPEG_QUALITY,
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise SnapshotError(f"ffmpeg не найден: {exc}") from exc

    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=_GRAB_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise SnapshotError("Таймаут получения кадра из записи") from exc

    return stdout


async def get_snapshot(path: str, seek_seconds: float | None = None) -> bytes:
    seek = seek_seconds if seek_seconds is not None else DEFAULT_SEEK_SECONDS
    data = await _grab(path, seek)
    if not data and seek != 0:
        data = await _grab(path, 0.0)
    if not data:
        raise SnapshotError("Не удалось получить кадр из записи")
    return data


async def get_cached_snapshot(
    cam_id: int, recording_id: str, path: str, seek_seconds: float | None
) -> bytes:
    """One cached JPEG per recording: whichever `seek` first materializes it wins,
and every later request for the same recording gets that same frame back."""
    # Intentionally share a single cached preview across all seek values for this recording.
    cache_path = _cache_path(cam_id, recording_id)

    if os.path.isfile(cache_path):
        with open(cache_path, "rb") as f:
            return f.read()

    async with _lock_for(cache_path):
        if os.path.isfile(cache_path):
            with open(cache_path, "rb") as f:
                return f.read()

        if not await is_valid_video(path):
            raise SnapshotError("Файл записи повреждён или не является видео")

        data = await get_snapshot(path, seek_seconds)

        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp_path = f"{cache_path}.tmp"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, cache_path)

        return data


async def startup() -> None:
    """Reaps hub-snapshot ffmpegs left by a previous run (registry + start time + env tag,
    never by name). No-op unless INGEST_ENABLED. Call once at app startup."""
    await _pool.startup()


async def stop_all(timeout: float = 4.0) -> None:
    """App shutdown: ends every running hub-snapshot ffmpeg (before ingest stops)."""
    await _pool.stop_all(timeout)


def _hub_snapshot_args() -> list[str]:
    # Same picture settings as the RTSP grab. Every frame of the stored GOP is decoded and
    # encoded (the newest one is kept): `-frames:v 1` would give the oldest, up to a GOP
    # (~3 s) stale. No `-fflags nobuffer`: with pipe input ffmpeg 8 then drops the packets
    # it consumed while probing, i.e. the start of the GOP. No `-re`: faster than real time.
    return [
        settings.FFMPEG_BINARY,
        "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-analyzeduration", "1000000", "-probesize", "1000000",
        "-i", "pipe:0",
        "-map", "0:v:0", "-an",
        "-vf", _SNAPSHOT_SCALE_FILTER,
        "-q:v", _JPEG_QUALITY,
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]


def _last_jpeg(data: bytes) -> bytes | None:
    """The last complete JPEG of an mjpeg stream (0xFF in entropy-coded data is always
    stuffed with 0x00, so SOI never occurs inside an image)."""
    start = data.rfind(_JPEG_SOI)
    if start < 0:
        return None
    jpeg = data[start:].rstrip(b"\0")
    return jpeg if jpeg.endswith(_JPEG_EOI) else None


async def _grab_from_hub(cam: Cam) -> bytes | None:
    """One frame from the ingest hub (init + last GOP -> local ffmpeg, no RTSP connection).
    None = the hub cannot serve this camera now, caller uses the direct path. Never raises
    (other than cancellation)."""
    global _hub_sem
    started = time.monotonic()
    hub = await consumer.ready_hub(cam.id, settings.INGEST_SNAPSHOT_HUB_WAIT_SECONDS, need_gop=True)
    if hub is None:
        if settings.INGEST_ENABLED:
            logger.info("snapshot: cam %s hub not available, using direct RTSP", cam.id)
        return None
    lock = _hub_locks.get(cam.id)
    if lock is None:
        lock = _hub_locks[cam.id] = asyncio.Lock()
    owned = None
    try:
        async with lock:  # concurrent requests share one decode of the same GOP
            init, gop = hub.init_segment, list(hub.gop)
            if init is None or not gop:
                logger.warning("snapshot: cam %s hub has no GOP, using direct RTSP", cam.id)
                return None
            digest = hashlib.blake2b(init + gop[-1] + bytes([len(gop) & 0xFF]), digest_size=16).digest()
            cached = _hub_last.get(cam.id)
            if cached is not None and cached[0] == digest:
                logger.info("snapshot: cam %s source=hub first_byte_ms=%d (same GOP, cached)",
                            cam.id, (time.monotonic() - started) * 1000)
                return cached[1]
            if _hub_sem is None:
                _hub_sem = asyncio.Semaphore(max(1, settings.INGEST_SNAPSHOT_MAX_PARALLEL))
            async with _hub_sem:
                owned = await _pool.spawn(_hub_snapshot_args(), cam_id=cam.id, stdin=True)
                payload = init + b"".join(gop)
                stdout, stderr = await asyncio.wait_for(
                    owned.process.communicate(payload), timeout=settings.INGEST_SNAPSHOT_TIMEOUT_SECONDS
                )
            jpeg = _last_jpeg(stdout)
            if jpeg is None:
                err = mask_credentials(stderr.decode(errors="replace").strip())[-300:]
                logger.warning("snapshot: cam %s hub decode gave no frame (rc=%s): %s, using direct RTSP",
                               cam.id, owned.process.returncode, err or "no stderr")
                return None
            _hub_last[cam.id] = (digest, jpeg)
            logger.info("snapshot: cam %s source=hub first_byte_ms=%d (gop %d bytes, jpeg %d bytes)",
                        cam.id, (time.monotonic() - started) * 1000, len(payload), len(jpeg))
            return jpeg
    except asyncio.TimeoutError:
        logger.warning("snapshot: cam %s hub decode timed out after %gs, using direct RTSP",
                       cam.id, settings.INGEST_SNAPSHOT_TIMEOUT_SECONDS)
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("snapshot: cam %s hub source failed, using direct RTSP", cam.id)
        return None
    finally:
        if owned is not None:
            if owned.process.returncode is not None:
                owned.forget()  # exited and reaped by communicate()
            else:  # timeout / cancelled request: end it, even if we are being cancelled
                owned.terminate_soon()


async def get_live_snapshot(cam: Cam) -> bytes:
    """Get live snapshot from camera.

    Uses ONVIF GetSnapshot if available; then (INGEST_ENABLED, hub online) the last GOP
    of the ingest hub decoded locally; otherwise falls back to ffmpeg RTSP grab.
    """
    # Try ONVIF first if enabled
    if cam.onvif_enabled and cam.onvif_snapshot_enabled:
        try:
            from app.services import onvif_client
            logger.info(f"[Snapshot] Cam {cam.id} ({cam.name or 'unnamed'}): попытка получения через ONVIF")
            snapshot_data = await onvif_client.get_snapshot(cam)
            logger.info(f"[Snapshot] Cam {cam.id}: успешно получен ONVIF снэпшот ({len(snapshot_data)} bytes)")
            return snapshot_data
        except Exception as exc:
            logger.warning(f"[Snapshot] Cam {cam.id}: ONVIF снэпшот не удалось получить ({exc}), переключение на ffmpeg")

    # Ingest hub: no new connection to the camera
    if settings.INGEST_ENABLED:
        hub_data = await _grab_from_hub(cam)
        if hub_data:
            return hub_data

    # Fallback to ffmpeg RTSP grab
    logger.info(f"[Snapshot] Cam {cam.id} ({cam.name or 'unnamed'}): попытка получения через ffmpeg RTSP")
    t_rtsp = time.monotonic()
    snapshot_data = await _grab_from_rtsp(cam)
    logger.info("snapshot: cam %s source=rtsp first_byte_ms=%d", cam.id, (time.monotonic() - t_rtsp) * 1000)
    logger.info(f"[Snapshot] Cam {cam.id}: успешно получен ffmpeg снэпшот ({len(snapshot_data)} bytes)")
    return snapshot_data


async def _grab_from_rtsp(cam: Cam) -> bytes:
    """Grab a single frame from RTSP stream using ffmpeg."""
    if not cam.proto or not cam.addr:
        logger.error(f"[Snapshot] Cam {cam.id}: отсутствует RTSP конфигурация (proto={cam.proto}, addr={cam.addr})")
        raise SnapshotError("Камера не имеет настроенного RTSP адреса")

    # Build RTSP URL
    rtsp_url = build_rtsp_url(cam)
    safe_url = f"{cam.proto}://***:***@{cam.addr}:{cam.port}{cam.path}"
    logger.debug(f"[Snapshot] Cam {cam.id}: ffmpeg запрос к {safe_url}")

    args = [
        settings.FFMPEG_BINARY,
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-vf", _SNAPSHOT_SCALE_FILTER,
        "-q:v", _JPEG_QUALITY,
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        logger.error(f"[Snapshot] Cam {cam.id}: ffmpeg бинарник не найден ({settings.FFMPEG_BINARY})")
        raise SnapshotError(f"ffmpeg не найден: {exc}") from exc

    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=_GRAB_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        logger.error(f"[Snapshot] Cam {cam.id}: таймаут ffmpeg после {_GRAB_TIMEOUT_SECONDS}s")
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise SnapshotError("Таймаут получения кадра из RTSP") from exc

    if not stdout:
        logger.error(f"[Snapshot] Cam {cam.id}: ffmpeg вернул пустой ответ (returncode={process.returncode})")
        raise SnapshotError("Не удалось получить кадр из RTSP потока")

    logger.debug(f"[Snapshot] Cam {cam.id}: ffmpeg успешно вернул {len(stdout)} bytes")
    return stdout

