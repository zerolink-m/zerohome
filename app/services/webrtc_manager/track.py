from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Callable

import numpy as np
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError, VIDEO_CLOCK_RATE, VIDEO_TIME_BASE
from av import VideoFrame

from app.core.config import settings
from app.services.rtsp_url import mask_credentials
from app.services.webrtc_manager import process as procs
from app.services.webrtc_manager.audio import AudioPipe, MediaClock
from app.services.webrtc_manager.ice import WebRTCStreamError
from app.services.webrtc_manager.probe import DEFAULT_FPS, ProbeError, StreamInfo, has_audio, probe_fmp4

logger = logging.getLogger(__name__)

_FFPROBE_TIMEOUT_SECONDS = 10.0  # direct path only
_STDIN_DRAIN_TIMEOUT_SECONDS = 10.0  # decoder not reading its input this long -> give up
_BURST_GAP_SECONDS = 0.05  # frames closer than this belong to one burst (one decoded fragment)
_GOP_WAIT_SECONDS = 5.0  # hub online but no fragment stored yet: wait this long for one
_HARD_BACKLOG_SECONDS = 3.0  # reader-side cap of decoded frames waiting for recv()


class _HubUnavailable(Exception):
    """The hub cannot serve this camera right now: use the direct RTSP path."""


def _frame_size(width: int, height: int) -> int:
    return width * height + 2 * ((width + 1) // 2) * ((height + 1) // 2)  # yuv420p


async def _hub_ready(cam_id: int):
    """The camera's CamHub when ingest is on, running for it and online (waiting up to
    INGEST_HUB_WAIT_SECONDS for that), else None."""
    if not settings.INGEST_ENABLED:
        return None
    from app.services.ingest import get_hub, get_supervisor  # late: avoids import cycles

    try:
        if not get_supervisor().is_running(cam_id):
            return None
        hub = get_hub().get(cam_id)
        if not (hub.online.is_set() and hub.init_segment is not None):
            await asyncio.wait_for(hub.online.wait(), timeout=settings.INGEST_HUB_WAIT_SECONDS)
        return hub if hub.init_segment is not None else None
    except asyncio.TimeoutError:
        return None
    except Exception:
        logger.exception("webrtc: hub check failed for cam %s", cam_id)
        return None


async def hub_has_audio(cam_id: int) -> bool:
    """Whether the camera's hub stream carries audio (decided before the SDP offer: no audio,
    no audio transceiver). False when the hub is not available (direct RTSP is video only)."""
    if not (settings.WEBRTC_AUDIO_ENABLED and cam_id):
        return False
    hub = await _hub_ready(cam_id)
    return hub is not None and hub.init_segment is not None and has_audio(hub.init_segment)


class RTSPVideoStreamTrack(VideoStreamTrack):
    """Raw video from a decoding ffmpeg -> aiortc.

    source "hub": the ffmpeg reads the ingest hub's fMP4 on stdin (size/fps come from a
    local ffprobe of the init segment, the camera is not touched). source "direct": the
    previous path (ffprobe + ffmpeg over RTSP).

    `on_end(track)` is called once when the stream ends by itself (hub restart/overflow,
    decoder died or stalled), never after stop(). `reconnect` tells whether a new
    session can be expected to work at once (the hub restarted) or not."""

    def __init__(self, rtsp_url: str, cam_id: int | None = None,
                 on_end: Callable[["RTSPVideoStreamTrack"], None] | None = None,
                 audio: bool = False):
        super().__init__()
        self.rtsp_url = rtsp_url
        self.cam_id = cam_id if cam_id is not None else 0
        self.on_end = on_end
        self.want_audio = audio  # the session has an audio track: decode the hub's audio too
        self.source: str = "direct"
        self.end_reason: str | None = None
        self.reconnect: bool = False
        self.owned: procs.OwnedProcess | None = None
        self.sub = None  # hub Subscription feeding the decoder's stdin
        self.audio: AudioPipe | None = None  # audio decoder fed with the same chunks (hub source only)
        self.clock = MediaClock()  # shared with the session's audio track: one timeline
        self._frames: deque[bytes] = deque()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._term_task: asyncio.Task | None = None
        self._last_stderr_line: str = ""
        self._start_lock = asyncio.Lock()
        self._started = False
        self._starting = False  # inside start(): an early end falls back instead of notifying
        self._stopped = False
        self._ended = False
        self._width = 0
        self._height = 0
        self._frame_size = 0
        self._fps: float = DEFAULT_FPS
        self._t_start = 0.0
        self._frames_in = 0
        self._frames_out = 0
        self._dropped = 0
        self._burst = 0  # frames that arrived back to back (a whole fragment is decoded at once)
        self._max_burst = 0
        self._last_arrival = 0.0
        self._last_sent = 0.0
        self._last_pts = -1

    # ---------------------------------------------------------------- start

    async def start(self) -> None:
        async with self._start_lock:
            if self._started or self._stopped:
                return
            self._t_start = time.monotonic()
            self._starting = True
            try:
                hub = await _hub_ready(self.cam_id) if self.cam_id else None
                if hub is not None:
                    try:
                        await self._start_hub(hub)
                        await self._await_first_frame()
                    except _HubUnavailable as exc:
                        logger.warning("webrtc: cam %s hub source failed (%s), falling back to direct",
                                       self.cam_id, exc)
                elif settings.INGEST_ENABLED and self.cam_id:
                    logger.info("webrtc: cam %s hub not available, using direct RTSP", self.cam_id)
                if not self._started:
                    await self._start_direct()
            finally:
                self._starting = False
            if self._ended and not self._stopped:  # ended during start(), nobody was told yet
                self._notify_end()

    async def _await_first_frame(self) -> None:
        """A hub decoder that dies or stays silent before its first frame (bad options,
        broken stream) is not a reason to give up: tear it down, use the direct path."""
        deadline = time.monotonic() + settings.WEBRTC_FIRST_FRAME_TIMEOUT_SECONDS
        while not self._frames_in and not self._ended and time.monotonic() < deadline:
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.01, deadline - time.monotonic()))
        if self._frames_in or self._stopped:
            return
        reason = self.end_reason or "no first frame"
        await self._teardown_hub()
        raise _HubUnavailable(f"decoder gave no frame: {reason}")

    async def _teardown_hub(self) -> None:
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.sub is not None:
            self.sub.close()
            self.sub = None
        if self.owned is not None:
            owned, self.owned = self.owned, None
            await owned.terminate()
        self._started = False
        self._ended = False
        self.end_reason = None
        self.reconnect = False
        self._frames.clear()
        self._wake.clear()
        self._frames_in = 0
        self._term_task = None
        if self.audio is not None:
            audio, self.audio = self.audio, None
            await audio.aclose()

    def _set_geometry(self, info: StreamInfo) -> None:
        self._width, self._height, self._fps = info.width, info.height, info.fps
        if not (0 < self._fps <= 120):
            self._fps = DEFAULT_FPS
        if self._width % 2 or self._height % 2:
            raise WebRTCStreamError(f"Неподдерживаемое разрешение потока: {self._width}x{self._height}")
        self._frame_size = _frame_size(self._width, self._height)

    def _decoder_args(self, source: str) -> list[str]:
        # No `-fflags nobuffer` with pipe input: ffmpeg 8 then drops the packets it consumed
        # while probing (the replayed GOP) and the HEVC decoder logs reference errors.
        # No `-re`: the replayed GOP is decoded faster than real time; recv() paces it.
        if source == "hub":
            return [
                settings.FFMPEG_BINARY,
                "-hide_banner", "-nostdin", "-loglevel", "warning",
                "-analyzeduration", "1000000", "-probesize", "1000000",
                "-i", "pipe:0",
                "-map", "0:v:0", "-an",
                "-f", "rawvideo", "-pix_fmt", "yuv420p",
                "-r", str(int(self._fps)),
                "pipe:1",
            ]
        return [
            settings.FFMPEG_BINARY,
            "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-f", "rawvideo", "-pix_fmt", "yuv420p",
            "-r", str(int(self._fps)),
            "pipe:1",
        ]

    async def _init_and_gop(self, hub):
        """The init segment, once the hub also holds a first fragment (the frame rate is only
        measurable from samples); gives up waiting after a few seconds and returns what it has."""
        deadline = time.monotonic() + _GOP_WAIT_SECONDS
        while not hub.gop and hub.init_segment is not None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        return hub.init_segment, list(hub.gop)

    async def _start_hub(self, hub) -> None:
        from app.services.ingest import get_hub  # late: avoids import cycles

        init, gop = await self._init_and_gop(hub)
        sub = get_hub().subscribe(self.cam_id)  # init + stored GOP are queued synchronously
        init, gop = hub.init_segment, list(hub.gop)  # the same snapshot the subscription holds
        try:
            if init is None:
                raise _HubUnavailable("no init segment")
            try:
                info = await probe_fmp4(init, gop)
            except (ProbeError, WebRTCStreamError) as exc:
                raise _HubUnavailable(f"cannot read stream geometry: {exc}") from exc
            if not info.fps_known:
                logger.warning("webrtc: cam %s frame rate unknown (no fragment yet), assuming %g fps",
                               self.cam_id, DEFAULT_FPS)
            try:
                self._set_geometry(info)
            except WebRTCStreamError as exc:
                raise _HubUnavailable(f"cannot read stream geometry: {exc}") from exc
            try:
                owned = await procs.spawn(self._decoder_args("hub"), cam_id=self.cam_id, stdin=True)
            except OSError as exc:
                raise _HubUnavailable(f"cannot start decoder: {exc}") from exc
        except BaseException:
            sub.close()
            raise
        self.source = "hub"
        self.sub = sub
        self.audio = await self._start_audio(init)
        self._begin(owned)

    async def _start_audio(self, init: bytes) -> AudioPipe | None:
        """The audio decoder, fed by _feed() with the chunks the video decoder gets (so both
        see the same init segment and GOP). Any failure here only means no sound."""
        if not (self.want_audio and settings.WEBRTC_AUDIO_ENABLED and has_audio(init)):
            return None
        pipe = AudioPipe(self.cam_id)
        try:
            await pipe.start()
        except Exception as exc:
            logger.warning("webrtc: cam %s audio decoder not started (%s), no sound", self.cam_id, exc)
            pipe.stop()
            return None
        return pipe

    async def _start_direct(self) -> None:
        probe_args = [
            settings.FFPROBE_BINARY,
            "-v", "error",
            "-rtsp_transport", "tcp",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate",
            "-of", "csv=p=0",
            self.rtsp_url,
        ]
        try:
            probe_proc = await asyncio.create_subprocess_exec(
                *probe_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise WebRTCStreamError(f"ffprobe не найден: {mask_credentials(str(exc))}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(probe_proc.communicate(), timeout=_FFPROBE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise WebRTCStreamError("ffprobe таймаут при проверке потока")
        finally:
            if probe_proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    probe_proc.kill()

        if probe_proc.returncode != 0:
            err_msg = mask_credentials(stderr.decode(errors="replace").strip())[:200] if stderr else "unknown error"
            raise WebRTCStreamError(f"ffprobe не смог прочитать поток: {err_msg}")

        # Parse ffprobe output: width,height,num/den
        try:
            parts = stdout.decode().strip().split(',')
            width, height = int(parts[0]), int(parts[1])
            fps_parts = parts[2].split('/')
            fps = float(fps_parts[0]) / float(fps_parts[1]) if len(fps_parts) == 2 else DEFAULT_FPS
        except (ValueError, IndexError, ZeroDivisionError) as exc:
            raise WebRTCStreamError(f"Не удалось распарсить ffprobe output: {exc}") from exc
        if width <= 0 or height <= 0:
            raise WebRTCStreamError(f"Некорректное разрешение потока: {width}x{height}")
        self._set_geometry(StreamInfo(width, height, fps))
        logger.info("webrtc: cam %s stream %dx%d @ %.2f fps (direct)", self.cam_id, width, height, self._fps)

        try:
            owned = await procs.spawn(self._decoder_args("direct"), cam_id=self.cam_id, stdin=False)
        except FileNotFoundError as exc:
            raise WebRTCStreamError(f"ffmpeg не найден: {mask_credentials(str(exc))}") from exc
        self.source = "direct"
        self._begin(owned)

    def _begin(self, owned: procs.OwnedProcess) -> None:
        self.owned = owned
        self._started = True
        if self._stopped:  # stop() raced with the start
            self._terminate_soon()
            return
        self._tasks = [
            asyncio.create_task(self._read_frames()),
            asyncio.create_task(self._drain_stderr()),
        ]
        if self.sub is not None:
            self._tasks.append(asyncio.create_task(self._feed()))

    # ------------------------------------------------------------- pipes

    async def _drain_stderr(self) -> None:
        """Drain ffmpeg stderr so it cannot block on a full pipe buffer."""
        stderr = self.owned.process.stderr
        if stderr is None:
            return
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    return
                text = mask_credentials(line.decode(errors="replace").strip())
                if text:
                    self._last_stderr_line = text[:500]
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _feed(self) -> None:
        """Hub subscription -> decoder stdin. Ending on its own (ingest restart, overflow,
        stop) ends this session: the stream timeline is gone, the client must reconnect
        (a new session starts on the new init segment)."""
        stdin = self.owned.process.stdin
        sub = self.sub
        try:
            async for chunk in sub:
                if self.audio is not None:
                    self.audio.push(chunk)
                stdin.write(chunk)
                await asyncio.wait_for(stdin.drain(), timeout=_STDIN_DRAIN_TIMEOUT_SECONDS)
            if self._ended:  # we closed the subscription ourselves (_finish/stop)
                return
            reason = f"ingest {sub.close_reason or 'closed'}"
            self.reconnect = True
        except (BrokenPipeError, ConnectionResetError):
            reason = "decoder stdin closed"
        except asyncio.TimeoutError:
            reason = "decoder not reading input"
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("webrtc: feeder for cam %s failed", self.cam_id)
            reason = "feeder error"
        logger.info("webrtc: cam %s hub feed ended (%s)", self.cam_id, reason)
        sub.close()
        with contextlib.suppress(Exception):
            stdin.close()
        if self.audio is not None:
            self.audio.end_input()
        self._finish(reason)

    async def _read_frames(self) -> None:
        """Raw yuv420p frames from the decoder's stdout."""
        stdout = self.owned.process.stdout
        hard_cap = max(2, int(self._fps * _HARD_BACKLOG_SECONDS))
        reason = None
        try:
            while True:
                timeout = settings.WEBRTC_STALL_SECONDS if self._frames_in else settings.WEBRTC_FIRST_FRAME_TIMEOUT_SECONDS
                try:
                    data = await asyncio.wait_for(stdout.readexactly(self._frame_size), timeout=timeout)
                except asyncio.TimeoutError:
                    reason = "no frames from decoder" if self._frames_in else "no first frame"
                    logger.warning("webrtc: cam %s source=%s %s for %gs, closing session",
                                   self.cam_id, self.source, reason, timeout)
                    break
                except asyncio.IncompleteReadError:
                    reason = "decoder ended"
                    break
                if self._frames_in == 0:
                    logger.info("webrtc: cam %s source=%s first_frame_ms=%d (%dx%d @ %.2f fps)",
                                self.cam_id, self.source, (time.monotonic() - self._t_start) * 1000,
                                self._width, self._height, self._fps)
                self._frames_in += 1
                arrived = time.monotonic()
                self._burst = self._burst + 1 if arrived - self._last_arrival < _BURST_GAP_SECONDS else 1
                self._max_burst = max(self._max_burst, self._burst)
                self._last_arrival = arrived
                self._frames.append(data)
                while len(self._frames) > hard_cap:
                    self._frames.popleft()
                    self._dropped += 1
                self._wake.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("webrtc: cam %s reader failed", self.cam_id)
            reason = "reader error"
        if reason == "decoder ended" and self._last_stderr_line:
            reason = f"decoder ended: {self._last_stderr_line}"
        self._finish(reason or "decoder ended")

    def _finish(self, reason: str) -> None:
        """The stream ended by itself (idempotent; silent after stop())."""
        if self._ended:
            return
        self._ended = True
        self.end_reason = self.end_reason or reason
        self._wake.set()
        if self.sub is not None:
            self.sub.close()  # release the hub subscription right away
        if not self._stopped:
            self._terminate_soon()
            if not self._starting:
                self._notify_end()

    def _notify_end(self) -> None:
        logger.info("webrtc: cam %s session stream ended: %s (frames=%d dropped=%d)",
                    self.cam_id, self.end_reason, self._frames_in, self._dropped)
        if self.on_end is not None:
            try:
                self.on_end(self)
            except Exception:
                logger.exception("webrtc: on_end callback failed")

    # -------------------------------------------------------------- frames

    async def recv(self) -> VideoFrame:
        """Called by aiortc to get next frame.

        Latency policy: the hub replays the last GOP at once, so frames arrive faster than
        real time, and the hub delivers whole fragments (one GOP), so even the live stream
        comes in bursts. Frames are handed out at most `fps` per second. Dropping every
        frame older than WEBRTC_MAX_LATENCY_MS would show half a second per GOP and then
        freeze until the next fragment, so the allowed backlog is that latency plus the
        largest burst seen (the backlog a smooth picture needs anyway). Anything beyond
        it is a consumer that fell behind: the oldest frames are dropped (the encoder
        re-encodes, no references to keep), and _read_frames caps the queue at a few
        seconds. Timestamps follow the wall clock, so drops do not speed playback up."""
        if not self._started:
            await self.start()

        while not self._frames:
            if self._ended or self._stopped:
                raise MediaStreamError
            self._wake.clear()
            await self._wake.wait()

        interval = 1.0 / self._fps
        wait = self._last_sent + interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        keep = max(1, int(self._fps * settings.WEBRTC_MAX_LATENCY_MS / 1000)) + self._max_burst
        while len(self._frames) > keep:
            self._frames.popleft()
            self._dropped += 1
        if not self._frames:  # stop() cleared it while we slept
            raise MediaStreamError
        data = self._frames.popleft()

        now = time.monotonic()
        self._last_sent = now
        pts = max(self._last_pts + 1, self.clock.stamp(now, VIDEO_CLOCK_RATE))
        self._last_pts = pts
        self._frames_out += 1

        frame = VideoFrame.from_ndarray(
            np.frombuffer(data, dtype=np.uint8).reshape(self._height * 3 // 2, self._width),
            format="yuv420p",
        )
        frame.pts = pts
        frame.time_base = VIDEO_TIME_BASE
        return frame

    # ---------------------------------------------------------------- stop

    def stop(self) -> None:
        """Stop the stream (called synchronously by aiortc and by close_session)."""
        self._stopped = True
        self._ended = True
        self._frames.clear()
        self._wake.set()
        for task in self._tasks:
            if not task.done() and task is not asyncio.current_task():
                task.cancel()
        if self.sub is not None:
            self.sub.close()
        if self.audio is not None:
            self.audio.stop()
        self._terminate_soon()

    def _terminate_soon(self) -> None:
        if self.audio is not None:
            self.audio.stop()  # the audio dies with the video, quietly
        if self.owned is None or self._term_task is not None:
            return
        try:
            self._term_task = asyncio.get_running_loop().create_task(self.owned.terminate())
        except RuntimeError:
            # No running loop: kill immediately rather than leak the process.
            self.owned.kill_now()
            self._term_task = None
            return
        procs.hold(self._term_task)

    async def aclose(self, timeout: float = 5.0) -> None:
        """stop() and wait (bounded) until the decoder process is gone."""
        self.stop()
        if self.audio is not None:
            await self.audio.aclose(timeout)
        task = self._term_task
        if task is None:
            return
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done and self.owned is not None:
            logger.warning("webrtc: decoder of cam %s did not stop in %gs, force-killing", self.cam_id, timeout)
            self.owned.kill_now()
