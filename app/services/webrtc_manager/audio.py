"""Camera audio for WebRTC: hub fMP4 -> ffmpeg (s16le mono 48 kHz) -> 20 ms AudioFrames.

The audio decoder belongs to the video track (RTSPVideoStreamTrack): it receives the very
same chunks the video decoder gets (init segment, stored GOP, live fragments), is started
and stopped with it, and shares its clock (MediaClock). aiortc encodes the PCM to Opus.

Sync with the video (one timeline, one source of truth):
 * both tracks stamp frames from ONE MediaClock: t0 = the first frame handed to aiortc by
   either of them; video pts = (now - t0) * 90000, audio pts = (due - t0) * 48000;
 * the audio waits for the video's first frame (t0), so both start at the head of the
   replayed GOP instead of audio running ahead by the video decode time;
 * the stale-data policy is the video's, in seconds: the backlog allowed in front of
   recv() is WEBRTC_MAX_LATENCY_MS plus the largest burst seen (one decoded fragment),
   the oldest samples beyond it are dropped (whole 20 ms frames), with the same hard cap
   of a few seconds on the reader side. Video and audio drop the same stretch of time.

Failure: if the audio decoder dies, stalls or is not fed, the track keeps sending silence
(frames stay paced, the video session is untouched); it ends only with the video."""
from __future__ import annotations

import asyncio
import contextlib
import fractions
import logging
import time

from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from av import AudioFrame

from app.core.config import settings
from app.services.webrtc_manager import process as procs

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 960  # 20 ms
FRAME_BYTES = SAMPLES_PER_FRAME * 2  # s16 mono
FRAME_SECONDS = SAMPLES_PER_FRAME / SAMPLE_RATE
BYTES_PER_SECOND = SAMPLE_RATE * 2
AUDIO_TIME_BASE = fractions.Fraction(1, SAMPLE_RATE)

_BURST_GAP_SECONDS = 0.05  # same meaning as in track.py
_HARD_BACKLOG_SECONDS = 3.0  # same cap as the video's
_STDIN_DRAIN_TIMEOUT_SECONDS = 10.0
_MAX_PENDING_INPUT_BYTES = 16 * 1024 * 1024  # fed but not yet read by ffmpeg: more -> give up on audio
_RESYNC_SECONDS = 0.1  # starved this long -> pts jumps to the wall clock instead of lagging


class MediaClock:
    """The shared origin of a session's media timestamps (wall clock, time.monotonic)."""

    def __init__(self) -> None:
        self.t0: float | None = None
        self.started = asyncio.Event()

    def stamp(self, now: float, rate: int) -> int:
        if self.t0 is None:
            self.t0 = now
            self.started.set()
        return int((now - self.t0) * rate)


def audio_args() -> list[str]:
    return [
        settings.FFMPEG_BINARY,
        "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-analyzeduration", "1000000", "-probesize", "1000000",
        "-i", "pipe:0",
        "-vn", "-map", "0:a:0",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
        "pipe:1",
    ]


class AudioPipe:
    """The audio ffmpeg of one session: push() fragments in, PCM accumulates in `buf`."""

    def __init__(self, cam_id: int) -> None:
        self.cam_id = cam_id
        self.owned: procs.OwnedProcess | None = None
        self.buf = bytearray()
        self.wake = asyncio.Event()
        self.dead = False
        self.reason: str | None = None
        self.stopped = False
        self.bytes_in = 0
        self.dropped_bytes = 0
        self.max_burst = 0  # bytes that arrived back to back (one decoded fragment)
        self._burst = 0
        self._last_arrival = 0.0
        self._pending = 0
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._term_task: asyncio.Task | None = None
        self._last_stderr_line = ""

    async def start(self) -> None:
        self.owned = await procs.spawn(audio_args(), cam_id=self.cam_id, stdin=True)
        self._tasks = [
            asyncio.create_task(self._feed()),
            asyncio.create_task(self._read()),
            asyncio.create_task(self._drain_stderr()),
        ]

    # ---- input (called from the video feeder; never blocks it)

    def push(self, chunk: bytes) -> None:
        if self.dead or self.stopped:
            return
        self._pending += len(chunk)
        if self._pending > _MAX_PENDING_INPUT_BYTES:
            self._fail("audio decoder not reading input")
            return
        self._queue.put_nowait(chunk)

    def end_input(self) -> None:
        if not (self.dead or self.stopped):
            self._queue.put_nowait(None)

    async def _feed(self) -> None:
        stdin = self.owned.process.stdin
        try:
            while True:
                chunk = await self._queue.get()
                if chunk is None:
                    break
                stdin.write(chunk)
                await asyncio.wait_for(stdin.drain(), timeout=_STDIN_DRAIN_TIMEOUT_SECONDS)
                self._pending -= len(chunk)
            with contextlib.suppress(Exception):
                stdin.close()
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionResetError):
            self._fail("audio decoder stdin closed")
        except asyncio.TimeoutError:
            self._fail("audio decoder not reading input")
        except Exception:
            logger.exception("webrtc: audio feeder for cam %s failed", self.cam_id)
            self._fail("audio feeder error")

    # ---- output

    async def _read(self) -> None:
        stdout = self.owned.process.stdout
        hard_cap = int(_HARD_BACKLOG_SECONDS * BYTES_PER_SECOND) // FRAME_BYTES * FRAME_BYTES
        reason = "audio decoder ended"
        try:
            while True:
                timeout = (settings.WEBRTC_STALL_SECONDS if self.bytes_in
                           else settings.WEBRTC_FIRST_FRAME_TIMEOUT_SECONDS)
                try:
                    data = await asyncio.wait_for(stdout.read(65536), timeout=timeout)
                except asyncio.TimeoutError:
                    reason = "no audio from decoder" if self.bytes_in else "no first audio"
                    break
                if not data:
                    if self._last_stderr_line:
                        reason = f"audio decoder ended: {self._last_stderr_line}"
                    break
                if not self.bytes_in:
                    logger.info("webrtc: cam %s audio first data", self.cam_id)
                self.bytes_in += len(data)
                arrived = time.monotonic()
                self._burst = self._burst + len(data) if arrived - self._last_arrival < _BURST_GAP_SECONDS else len(data)
                self.max_burst = max(self.max_burst, self._burst)
                self._last_arrival = arrived
                self.buf += data
                if len(self.buf) > hard_cap:
                    cut = (len(self.buf) - hard_cap + FRAME_BYTES - 1) // FRAME_BYTES * FRAME_BYTES
                    del self.buf[:cut]
                    self.dropped_bytes += cut
                self.wake.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("webrtc: cam %s audio reader failed", self.cam_id)
            reason = "audio reader error"
        if not self.stopped:
            self._fail(reason)

    async def _drain_stderr(self) -> None:
        stderr = self.owned.process.stderr
        if stderr is None:
            return
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    return
                text = line.decode(errors="replace").strip()
                if text:
                    self._last_stderr_line = text[:500]
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def take(self, keep_bytes: int) -> bytes | None:
        """Drops the oldest samples beyond `keep_bytes` (whole frames), then returns the
        oldest 20 ms frame, or None when there is none."""
        keep = max(FRAME_BYTES, keep_bytes // FRAME_BYTES * FRAME_BYTES)
        extra = (len(self.buf) - keep) // FRAME_BYTES * FRAME_BYTES
        if extra > 0:
            del self.buf[:extra]
            self.dropped_bytes += extra
        if len(self.buf) < FRAME_BYTES:
            return None
        data = bytes(self.buf[:FRAME_BYTES])
        del self.buf[:FRAME_BYTES]
        return data

    # ---- lifecycle

    def _fail(self, reason: str) -> None:
        """The audio is over (the video goes on): the track sends silence from now on."""
        if self.dead or self.stopped:
            return
        self.dead = True
        self.reason = reason
        logger.warning("webrtc: cam %s audio stopped (%s), continuing without sound", self.cam_id, reason)
        self.wake.set()
        self.terminate_soon()

    def stop(self) -> None:
        self.stopped = True
        self.wake.set()
        for task in self._tasks:
            if not task.done() and task is not asyncio.current_task():
                task.cancel()
        self.terminate_soon()

    def terminate_soon(self) -> None:
        if self.owned is None or self._term_task is not None:
            return
        try:
            self._term_task = asyncio.get_running_loop().create_task(self.owned.terminate())
        except RuntimeError:
            self.owned.kill_now()
            return
        procs.hold(self._term_task)

    async def aclose(self, timeout: float = 5.0) -> None:
        self.stop()
        task = self._term_task
        if task is None:
            return
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done and self.owned is not None:
            logger.warning("webrtc: audio decoder of cam %s did not stop in %gs, force-killing",
                           self.cam_id, timeout)
            self.owned.kill_now()


class RTSPAudioStreamTrack(MediaStreamTrack):
    """Audio of the session's video track as 20 ms mono s16 48 kHz frames (aiortc -> Opus).

    Created only when the stream has an audio track (decided before the offer). Frames come
    from the video track's AudioPipe; without one (direct RTSP source, audio decoder dead)
    the track sends silence for as long as the video lives."""

    kind = "audio"

    def __init__(self, video) -> None:
        super().__init__()
        self.video = video
        self._stopped = False
        self._next_pts: int | None = None
        self._frames_out = 0

    async def recv(self) -> AudioFrame:
        video = self.video
        if self._stopped:
            raise MediaStreamError
        if not video._started:
            try:
                await video.start()
            except Exception as exc:  # the video recv() reports the cause to the session
                raise MediaStreamError from exc
        clock = video.clock
        if self._next_pts is None and clock.t0 is None:
            # start together with the video: both begin at the head of the replayed GOP
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(clock.started.wait(), timeout=settings.WEBRTC_FIRST_FRAME_TIMEOUT_SECONDS)

        data: bytes | None = None
        while True:
            if self._stopped or video._stopped or video._ended:
                raise MediaStreamError
            pipe: AudioPipe | None = video.audio
            if pipe is None or pipe.dead:
                break  # silence
            keep = int((settings.WEBRTC_MAX_LATENCY_MS / 1000) * BYTES_PER_SECOND) + pipe.max_burst
            data = pipe.take(keep)
            if data is not None:
                break
            pipe.wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(pipe.wake.wait(), timeout=0.5)

        # pts defines when the frame is due: counter based (steady 960 steps), pulled
        # forward to the wall clock after a starvation, never backwards.
        now = time.monotonic()
        expected = clock.stamp(now, SAMPLE_RATE)
        if self._next_pts is None or expected - self._next_pts > _RESYNC_SECONDS * SAMPLE_RATE:
            pts = expected
        else:
            pts = self._next_pts
        wait = clock.t0 + pts / SAMPLE_RATE - now
        if wait > 0:
            await asyncio.sleep(wait)
        if self._stopped or video._stopped:
            raise MediaStreamError
        self._next_pts = pts + SAMPLES_PER_FRAME
        self._frames_out += 1
        return make_frame(data, pts)

    def stop(self) -> None:
        self._stopped = True
        super().stop()


def make_frame(data: bytes | None, pts: int) -> AudioFrame:
    frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
    frame.planes[0].update(data if data is not None else bytes(FRAME_BYTES))
    frame.sample_rate = SAMPLE_RATE
    frame.pts = pts
    frame.time_base = AUDIO_TIME_BASE
    return frame
