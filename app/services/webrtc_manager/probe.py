"""Stream geometry from the ingest hub's own fMP4 (no camera, no network).

A local `ffprobe -i pipe:0` over the init segment (+ the first stored fragments) takes
milliseconds. The result is cached by the init segment's hash: a new ingest process
produces a new init segment, an unchanged one means unchanged geometry."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass

from app.core.config import settings

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_FPS = 25.0
_MAX_PROBE_BYTES = 1024 * 1024
_MAX_PROBE_FRAGMENTS = 3
_CACHE_SIZE = 64


class ProbeError(Exception):
    pass


@dataclass(frozen=True)
class StreamInfo:
    width: int
    height: int
    fps: float
    fps_known: bool = True  # False: no fragment was probed, the init segment alone has no frame rate


_cache: "OrderedDict[str, StreamInfo]" = OrderedDict()


def _parse_rate(value: object) -> float | None:
    try:
        num, _, den = str(value).partition("/")
        rate = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if 0 < rate <= 120 else None


def parse_probe_json(text: str) -> StreamInfo:
    try:
        stream = json.loads(text)["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProbeError(f"unparseable ffprobe output: {exc}") from exc
    fps = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate")) or DEFAULT_FPS
    return StreamInfo(width, height, fps)


async def probe_fmp4(init: bytes, fragments: list[bytes] | None = None) -> StreamInfo:
    """Width/height/fps of the video stream in `init` (+ leading `fragments`)."""
    key = hashlib.sha1(init).hexdigest()
    cached = _cache.get(key)
    if cached is not None:  # only ever holds results with a measured fps
        _cache.move_to_end(key)
        return cached

    data = bytearray(init)
    for fragment in (fragments or [])[:_MAX_PROBE_FRAGMENTS]:
        if len(data) + len(fragment) > _MAX_PROBE_BYTES:
            break
        data += fragment
    args = [
        settings.FFPROBE_BINARY, "-v", "error",
        "-analyzeduration", "1000000", "-probesize", "1000000",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate",
        "-of", "json", "-i", "pipe:0",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ProbeError(f"ffprobe cannot start: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(bytes(data)), timeout=PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise ProbeError("ffprobe timed out on the init segment") from exc
    finally:
        if proc.returncode is None:  # our own unreaped child: kill via the handle
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed: {stderr.decode(errors='replace').strip()[:200]}")
    info = parse_probe_json(stdout.decode(errors="replace"))
    if info.width <= 0 or info.height <= 0:
        raise ProbeError(f"bad geometry {info.width}x{info.height}")
    if not fragments:
        # An empty-moov init has no sample table: ffprobe reports a made-up rate (25).
        return StreamInfo(info.width, info.height, info.fps, fps_known=False)
    _cache[key] = info
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return info
