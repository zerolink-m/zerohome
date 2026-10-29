"""Motion detection service using YOLOv8."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, Optional

import cv2
import numpy as np

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.cams import Cam
from app.models.motion_event import MotionEvent
from app.models.push_settings import PushSettings
from app.models.user import User
from app.services import license_plate_ocr, push
from app.services.object_tracker import ObjectTracker

logger = logging.getLogger("app.services.motion_detection")
from app.services.ingest import consumer
from app.services.motion_detection.yolo import YOLO_CLASSES, _get_yolo_model
from app.services.rtsp_url import mask_credentials
from app.services.webrtc_manager.probe import ProbeError, probe_fmp4

# Owned ffmpeg children of detection sessions (hub decoders and direct RTSP grabbers),
# separate registry file (motion_registry.json); see ingest/consumer.py.
pool = consumer.OwnedProcessPool("motion", "motion_registry.json")

_OUT_FPS = 1.0  # detection runs on one frame per second
_BURST_GAP_SECONDS = 0.25  # frames closer than this came out of one hub fragment (a whole GOP at once)
_STDIN_DRAIN_TIMEOUT_SECONDS = 10.0  # decoder not reading its input this long -> give up
_RETRY_INITIAL_SECONDS = 1.0
_RETRY_MAX_SECONDS = 30.0


class _HubUnavailable(Exception):
    """The hub cannot feed detection right now (before the first frame): use direct RTSP."""


def _hub_decoder_args() -> list[str]:
    # fps=1 and the native size, like the direct path. No `-fflags nobuffer` with pipe input
    # (ffmpeg 8 then drops the packets consumed while probing); no `-re` (a fragment is
    # decoded faster than real time, it is a whole GOP at once). `-threads 1`: frame-threaded
    # HEVC decoding holds back ~20 frames (measured, ffmpeg 8.0.1) until more input arrives,
    # i.e. a whole extra GOP of delay; one decoding thread outputs a fragment right away.
    return [
        settings.FFMPEG_BINARY,
        "-hide_banner", "-nostdin", "-loglevel", "warning",
        "-threads", "1",
        "-analyzeduration", "1000000", "-probesize", "1000000",
        "-i", "pipe:0",
        "-map", "0:v:0", "-an",
        "-vf", "fps=1",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "pipe:1",
    ]


def _trace_box(results, box, cls: int, conf: float, fate: str, obj_size: float | None = None) -> str:
    """One raw YOLO detection for the MOTION_DEBUG_LOG line (only called when it is on)."""
    names = getattr(results, "names", None)
    name = names.get(cls) if isinstance(names, dict) else None
    name = YOLO_CLASSES.get(cls) or name or f"cls{cls}"
    try:
        x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].cpu().numpy())
        where = f" bbox=({x1},{y1},{x2 - x1},{y2 - y1})"
    except Exception:
        where = ""
    size = f" size={obj_size:.4f}" if obj_size is not None else ""
    return f"{name}(id={cls}) conf={conf:.2f}{where}{size} {fate}"


class MotionDetectionSession:
    """Motion detection session for a camera.

    Frame source: with INGEST_ENABLED and an online hub, a local ffmpeg decodes the hub's
    fMP4 (stdin) - the camera is not touched; otherwise (or when the hub fails before the
    first frame) the previous path: own ffprobe + ffmpeg over RTSP. A source that ends is
    restarted a few times (INGEST_MOTION_RESTART_ATTEMPTS failures in a row end the session)."""

    def __init__(self, cam: Cam, user_settings: list[PushSettings]):
        self.cam = cam
        self.user_settings = user_settings
        self.tracker = ObjectTracker(max_age=2.0, iou_threshold=0.3)
        self.last_notification_time: Dict[int, float] = {}  # user_id -> timestamp
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        self.ffmpeg_process: Optional[asyncio.subprocess.Process] = None  # current frame source
        self.source: str | None = None  # "hub" | "rtsp" while running
        self._owned: set[consumer.OwnedProcess] = set()

    async def start(self):
        """Start motion detection."""
        if self.is_running:
            logger.debug(f"[Cam {self.cam.id}] Already running, skipping start")
            return

        self.is_running = True
        self.task = asyncio.create_task(self._run())
        logger.info(f"[Cam {self.cam.id}] Started motion detection session")
        logger.debug(f"[Cam {self.cam.id}] Tracker settings: max_age=2.0s, iou_threshold=0.3")
        logger.debug(f"[Cam {self.cam.id}] Monitoring for {len(self.user_settings)} user(s)")

    async def stop(self):
        """Stop motion detection."""
        self.is_running = False

        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"[Cam {self.cam.id}] Error stopping detection task: {exc}")

        # normally the task's own cleanup did this; whatever it left is ended here
        for owned in list(self._owned):
            try:
                await owned.terminate()
            except Exception as exc:
                logger.warning(f"Error stopping ffmpeg: {exc}")
        self._owned.clear()

        logger.info(f"Stopped motion detection for cam {self.cam.id}")

    async def _run(self):
        """Main detection loop: runs the frame source, restarts it when it ends."""
        try:
            attempts = 0
            delay = _RETRY_INITIAL_SECONDS
            while self.is_running:
                frames = await self._run_once()
                if not self.is_running:
                    break
                if frames:
                    attempts = 0
                    delay = _RETRY_INITIAL_SECONDS
                else:
                    attempts += 1
                if attempts >= settings.INGEST_MOTION_RESTART_ATTEMPTS:
                    logger.error(f"[Cam {self.cam.id}] Frame source failed {attempts} times in a row, "
                                 f"motion detection session ends")
                    break
                logger.warning(f"[Cam {self.cam.id}] Frame source ended after {frames} frame(s), "
                               f"restarting in {delay:g}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_SECONDS)
        except Exception as exc:
            logger.error(f"[Cam {self.cam.id}] Motion detection error: {exc}", exc_info=True)
        finally:
            self.is_running = False
            logger.debug(f"[Cam {self.cam.id}] Detection loop exited")

    async def _run_once(self) -> int:
        """One frame source from start to end; returns how many frames it decoded."""
        # the model is loaded before any pipe is opened (loading takes seconds)
        model = await asyncio.to_thread(_get_yolo_model)
        hub = await consumer.ready_hub(self.cam.id, settings.INGEST_HUB_WAIT_SECONDS)
        if hub is not None:
            try:
                return await self._run_hub(hub, model)
            except _HubUnavailable as exc:
                logger.warning(f"[Cam {self.cam.id}] hub source failed ({exc}), falling back to direct RTSP")
        elif settings.INGEST_ENABLED:
            logger.info(f"[Cam {self.cam.id}] hub not available, using direct RTSP")
        return await self._run_direct(model)

    # ------------------------------------------------------------------ helpers

    async def _drain_stderr(self, owned: consumer.OwnedProcess, sink: list[str]) -> None:
        """Keeps ffmpeg from blocking on a full stderr pipe; remembers the last line."""
        stderr = owned.process.stderr
        if stderr is None:
            return
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    return
                text = mask_credentials(line.decode(errors="replace").strip())
                if text:
                    sink[:] = [text[:500]]
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _finish_process(self, owned: consumer.OwnedProcess | None, tasks: list[asyncio.Task]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if owned is not None:
            try:
                await owned.terminate()
            finally:
                self._owned.discard(owned)
        self.ffmpeg_process = None
        self.source = None

    # ------------------------------------------------------------------ direct RTSP

    async def _run_direct(self, model) -> int:
        """The previous path: ffprobe for the size, one RTSP ffmpeg at 1 fps."""
        owned: consumer.OwnedProcess | None = None
        tasks: list[asyncio.Task] = []
        frames = 0
        try:
            # Build RTSP URL
            rtsp_url = f"{self.cam.proto}://{self.cam.username}:{self.cam.password}@{self.cam.addr}:{self.cam.port}{self.cam.path}"
            logger.debug(f"[Cam {self.cam.id}] Connecting to RTSP: {self.cam.proto}://{self.cam.addr}:{self.cam.port}{self.cam.path}")

            # Use ffprobe to detect resolution
            probe_cmd = [
                settings.FFPROBE_BINARY,
                '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height',
                '-of', 'csv=p=0',
                '-rtsp_transport', 'tcp',
                rtsp_url
            ]

            logger.debug(f"[Cam {self.cam.id}] Probing stream resolution...")
            probe_started = time.monotonic()
            probe_proc = await asyncio.create_subprocess_exec(
                *probe_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(probe_proc.communicate(), timeout=10.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    probe_proc.kill()
                logger.error(f"[Cam {self.cam.id}] Timeout while probing stream")
                return 0

            if probe_proc.returncode != 0:
                logger.error(f"[Cam {self.cam.id}] ffprobe failed: {mask_credentials(stderr.decode(errors='replace'))}")
                # Fallback to common resolution
                width, height = 1280, 720
                logger.warning(f"[Cam {self.cam.id}] Using fallback resolution: {width}x{height}")
            else:
                parts = stdout.decode().strip().split(',')
                width, height = int(parts[0]), int(parts[1])
                logger.info(f"[Cam {self.cam.id}] Detected resolution: {width}x{height}")

            # Start ffmpeg to grab frames
            cmd = [
                settings.FFMPEG_BINARY,
                '-rtsp_transport', 'tcp',
                '-i', rtsp_url,
                '-vf', 'fps=1',  # 1 frame per second
                '-f', 'image2pipe',
                '-pix_fmt', 'bgr24',
                '-vcodec', 'rawvideo',
                '-s', f'{width}x{height}',  # Force resolution
                'pipe:1'
            ]

            logger.debug(f"[Cam {self.cam.id}] Starting ffmpeg process (fps=1, resolution={width}x{height}, format=rawvideo)")
            owned = await pool.spawn(cmd, cam_id=self.cam.id, stdin=False)
            self._owned.add(owned)
            self.ffmpeg_process = owned.process
            self.source = "rtsp"
            last_err: list[str] = []
            tasks.append(asyncio.create_task(self._drain_stderr(owned, last_err)))

            frame_count = 0
            frame_size = width * height * 3
            logger.debug(f"[Cam {self.cam.id}] YOLO model loaded, frame_size={frame_size} bytes, starting capture loop")

            while self.is_running:
                try:
                    # readexactly: a plain read(n) returns whatever the pipe holds (a few
                    # hundred KB), i.e. a "truncated" frame for any real resolution
                    raw_frame = await owned.process.stdout.readexactly(frame_size)
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        logger.warning(f"[Cam {self.cam.id}] Incomplete frame (got {len(exc.partial)}/{frame_size} bytes)")
                    else:
                        logger.warning(f"[Cam {self.cam.id}] ffmpeg ended: {last_err[0] if last_err else 'no details'}")
                    break
                if frame_count == 0:
                    logger.info(f"[Cam {self.cam.id}] source=rtsp first_byte_ms={(time.monotonic() - probe_started) * 1000:.0f} "
                                f"({width}x{height})")

                # Convert to numpy array
                frame = np.frombuffer(raw_frame, dtype=np.uint8).reshape((height, width, 3))
                frame_count += 1
                frames = frame_count
                logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count} captured ({width}x{height})")

                # Run detection every frame (already at 1fps)
                await self._process_frame(frame, model, frame_count, time.time())
        finally:
            await self._finish_process(owned, tasks)
        return frames

    # ------------------------------------------------------------------ hub

    async def _run_hub(self, hub, model) -> int:
        """Decode the hub's stream locally. Raises _HubUnavailable only before the first
        frame (the caller then falls back to RTSP); afterwards returns the frame count."""
        from app.services.ingest import get_hub  # late: avoids import cycles

        started = time.monotonic()
        if hub.init_segment is None:
            raise _HubUnavailable("no init segment")
        # The hub replays init + the stored GOP first; those frames are up to a GOP old, so
        # the decoder gets the init and only the fragments that arrive after subscribing
        # (each starts at a keyframe). Counted here, in the same step as the subscription.
        n_stale = len(hub.gop)
        sub = get_hub().subscribe(self.cam.id)
        init, gop = hub.init_segment, list(hub.gop)
        owned: consumer.OwnedProcess | None = None
        tasks: list[asyncio.Task] = []
        frames = 0
        try:
            if init is None:
                raise _HubUnavailable("no init segment")
            try:
                info = await probe_fmp4(init, gop)
            except ProbeError as exc:
                raise _HubUnavailable(f"cannot read stream geometry: {exc}") from exc
            width, height = info.width, info.height
            try:
                owned = await pool.spawn(_hub_decoder_args(), cam_id=self.cam.id, stdin=True)
            except OSError as exc:
                raise _HubUnavailable(f"cannot start decoder: {exc}") from exc
            self._owned.add(owned)
            self.ffmpeg_process = owned.process
            self.source = "hub"
            last_err: list[str] = []
            tasks.append(asyncio.create_task(self._drain_stderr(owned, last_err)))
            tasks.append(asyncio.create_task(self._feed(sub, owned, n_stale)))
            logger.debug(f"[Cam {self.cam.id}] hub decoder started ({width}x{height}), skipping {n_stale} stored fragment(s)")
            frames = await self._read_hub_frames(owned, model, width, height, started, last_err)
        finally:
            sub.close()
            await self._finish_process(owned, tasks)
        return frames

    async def _feed(self, sub, owned: consumer.OwnedProcess, n_stale: int) -> None:
        """Hub subscription -> decoder stdin: the init segment, then live fragments (the
        n_stale stored ones right after the init are dropped). Ending on its own (ingest
        restart/overflow/stop) closes stdin: the decoder flushes and exits."""
        stdin = owned.process.stdin
        index = 0
        try:
            async for chunk in sub:
                stale = 1 <= index <= n_stale
                index += 1
                if stale:
                    continue
                stdin.write(chunk)
                await asyncio.wait_for(stdin.drain(), timeout=_STDIN_DRAIN_TIMEOUT_SECONDS)
            logger.info(f"[Cam {self.cam.id}] hub feed ended ({sub.close_reason or 'closed'})")
        except (BrokenPipeError, ConnectionResetError):
            logger.info(f"[Cam {self.cam.id}] decoder stdin closed")
        except asyncio.TimeoutError:
            logger.warning(f"[Cam {self.cam.id}] decoder not reading its input for {_STDIN_DRAIN_TIMEOUT_SECONDS:g}s")
            owned.terminate_soon()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"[Cam {self.cam.id}] hub feeder failed")
        finally:
            sub.close()
            with contextlib.suppress(Exception):
                stdin.close()

    async def _read_hub_frames(self, owned, model, width: int, height: int, started: float,
                               last_err: list[str]) -> int:
        """Frames out of the decoder. They come in bursts (a fragment is decoded at once),
        so each burst gets estimated capture times: the last frame arrived now, earlier
        ones 1/fps apart before it. Frames too old are skipped (no analysis, no push)."""
        stdout = owned.process.stdout
        frame_size = width * height * 3
        count = 0
        skipped = 0
        last_ts = 0.0
        eof = False
        while self.is_running and not eof:
            timeout = (settings.INGEST_MOTION_STALL_SECONDS if count
                       else settings.INGEST_MOTION_FIRST_FRAME_TIMEOUT_SECONDS)
            try:
                data = await asyncio.wait_for(stdout.readexactly(frame_size), timeout=timeout)
            except asyncio.TimeoutError:
                if not count:
                    raise _HubUnavailable(f"no first frame in {timeout:g}s: {last_err[0] if last_err else 'no stderr'}")
                logger.warning(f"[Cam {self.cam.id}] hub decoder silent for {timeout:g}s, restarting source")
                break
            except asyncio.IncompleteReadError as exc:
                if not count:
                    raise _HubUnavailable(f"decoder ended before the first frame: {last_err[0] if last_err else 'no stderr'}")
                if exc.partial:
                    logger.warning(f"[Cam {self.cam.id}] Incomplete frame (got {len(exc.partial)}/{frame_size} bytes)")
                break
            if not count:
                logger.info(f"[Cam {self.cam.id}] source=hub first_byte_ms={(time.monotonic() - started) * 1000:.0f} "
                            f"({width}x{height})")
            batch = [(data, time.time())]
            while True:  # the rest of this burst
                try:
                    more = await asyncio.wait_for(stdout.readexactly(frame_size), timeout=_BURST_GAP_SECONDS)
                except asyncio.TimeoutError:
                    break
                except asyncio.IncompleteReadError:
                    eof = True
                    break
                batch.append((more, time.time()))
            arrived = batch[-1][1]
            for k, (raw_frame, _) in enumerate(batch):
                if not self.is_running:
                    break
                count += 1
                ts = arrived - (len(batch) - 1 - k) / _OUT_FPS
                ts = min(max(ts, last_ts + 0.001), time.time())  # never back in time (nor in the future)
                last_ts = ts
                if time.time() - ts > settings.INGEST_MOTION_MAX_FRAME_AGE_SECONDS:
                    skipped += 1
                    logger.info(f"[Cam {self.cam.id}] Frame #{count} is {time.time() - ts:.1f}s old, skipped ({skipped} so far)")
                    continue
                frame = np.frombuffer(raw_frame, dtype=np.uint8).reshape((height, width, 3))
                logger.debug(f"[Cam {self.cam.id}] Frame #{count} decoded ({width}x{height}, burst of {len(batch)})")
                await self._process_frame(frame, model, count, ts)
        return count

    async def _process_frame(self, frame: np.ndarray, model, frame_count: int, frame_ts: float | None = None):
        """Process a single frame. `frame_ts`: its capture time (default: now); frames that
        come in bursts from the hub carry estimated times so the tracker, the cooldown and
        the event time are not skewed by delivery."""
        if frame_ts is None:
            frame_ts = time.time()
        # MOTION_DEBUG_LOG: one INFO line per inference (see _log_frame_debug); nothing is
        # collected or formatted when the switch is off.
        trace: list | None = [] if settings.MOTION_DEBUG_LOG else None
        verdict: list[str] = ["no_event: error"]
        infer_ms = 0.0
        events = 0
        results = None
        try:
            # Run YOLO detection
            logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: Running YOLO inference...")
            if trace is not None:
                _t0 = time.perf_counter()
            results = model(frame, verbose=False)[0]
            if trace is not None:
                infer_ms = (time.perf_counter() - _t0) * 1000

            # Extract detections
            detections = []
            raw_detections_count = len(results.boxes)
            logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: YOLO detected {raw_detections_count} objects")

            for box in results.boxes:
                cls = int(box.cls[0])
                conf = float(box.conf[0])

                # Check if it's a class we care about
                if cls not in YOLO_CLASSES:
                    if trace is not None:
                        trace.append(_trace_box(results, box, cls, conf, "dropped: class not in monitored list"))
                    logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: Skipping class_id={cls} (not in monitored classes)")
                    continue

                object_type = YOLO_CLASSES[cls]

                # Check if user wants this type
                if not self._should_detect_type(object_type):
                    if trace is not None:
                        trace.append(_trace_box(results, box, cls, conf, "dropped: type disabled in user settings"))
                    logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: Skipping {object_type} (user settings disabled)")
                    continue

                # Get bounding box
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                x, y, w, h = int(x1), int(y1), int(x2 - x1), int(y2 - y1)

                # Calculate object size (% of frame)
                frame_area = frame.shape[0] * frame.shape[1]
                obj_area = w * h
                obj_size = obj_area / frame_area

                # Filter by minimum size and confidence
                min_confidence = self._get_min_confidence()
                min_size = self._get_min_object_size()

                if conf < min_confidence:
                    if trace is not None:
                        trace.append(_trace_box(results, box, cls, conf, f"dropped: conf<{min_confidence:.2f}", obj_size))
                    logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: {object_type} filtered (conf={conf:.2f} < {min_confidence:.2f})")
                    continue

                if obj_size < min_size:
                    if trace is not None:
                        trace.append(_trace_box(results, box, cls, conf, f"dropped: size<{min_size:.4f}", obj_size))
                    logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: {object_type} filtered (size={obj_size:.4f} < {min_size:.4f})")
                    continue

                logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: Detected {object_type} conf={conf:.2f} size={obj_size:.4f} bbox=({x},{y},{w},{h})")
                detections.append((object_type, (x, y, w, h), conf, obj_size))
                if trace is not None:
                    trace.append(_trace_box(results, box, cls, conf, "passed filters", obj_size))

            if not detections:
                verdict[0] = ("no_event: yolo found nothing" if not raw_detections_count
                              else "no_event: all detections dropped by filters")
                logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: No valid detections after filtering")
                return

            logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: {len(detections)} detections passed filters, updating tracker...")

            # Update tracker
            tracker_input = [(obj_type, bbox, conf) for obj_type, bbox, conf, _ in detections]
            self.tracker.update(tracker_input, now=frame_ts)

            # Get moving objects
            moving_objects = self.tracker.get_moving_objects(min_tracking_time=0.5, now=frame_ts)
            logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: Tracker reports {len(moving_objects)} moving objects")
            verdict[0] = ("no_event: tracker - no moving object (needs tracked >=0.5s and >50px x/y shift "
                          "between first and last bbox)")

            # Process each moving object
            for tracked_obj in moving_objects:
                logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: Processing tracked {tracked_obj.object_type} (id={tracked_obj.object_id}, age={tracked_obj.age:.1f}s)")

                # Check cooldown
                if not self._check_cooldown(tracked_obj.object_type, frame_ts):
                    logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: {tracked_obj.object_type} in cooldown, skipping notification")
                    verdict[0] = f"no_event: cooldown ({tracked_obj.object_type})"
                    continue

                # Get latest bbox and confidence
                bbox = tracked_obj.get_latest_bbox()
                confidence = tracked_obj.get_average_confidence()
                x, y, w, h = bbox
                obj_size = (w * h) / (frame.shape[0] * frame.shape[1])

                logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: {tracked_obj.object_type} passed cooldown, avg_conf={confidence:.2f}")

                # Extract license plate if it's a car
                license_plate = None
                if tracked_obj.object_type == 'car' and self._should_detect_license_plate():
                    logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: Running OCR for car license plate...")
                    license_plate = await asyncio.to_thread(
                        license_plate_ocr.extract_license_plate,
                        frame,
                        bbox
                    )
                    if license_plate:
                        logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: License plate detected: {license_plate}")
                    else:
                        logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: No license plate found")

                # Save event and notify
                logger.debug(f"[Cam {self.cam.id}] Frame #{frame_count}: Saving event and notifying users...")
                await self._save_and_notify(
                    frame,
                    tracked_obj.object_type,
                    bbox,
                    confidence,
                    obj_size,
                    license_plate,
                    frame_ts,
                )
                events += 1  # the save was attempted (a DB/disk failure is logged by _save_and_notify)
                verdict[0] = ""

        except Exception as exc:
            verdict[0] = f"no_event: error {exc!r}"
            logger.error(f"[Cam {self.cam.id}] Frame #{frame_count}: Processing error: {exc}", exc_info=True)
        finally:
            if trace is not None:
                self._log_frame_debug(frame, frame_count, infer_ms, trace, verdict[0], events)

    def _log_frame_debug(self, frame, frame_count: int, infer_ms: float, trace: list, verdict: str,
                         events: int) -> None:
        """MOTION_DEBUG_LOG: what YOLO said for this frame (before filtering) and what became of it."""
        try:
            h, w = frame.shape[:2]
            dets = "; ".join(trace) if trace else ""
            result = verdict or "event"
            logger.info(f"[Cam {self.cam.id}] Frame #{frame_count} yolo {w}x{h} infer_ms={infer_ms:.0f} "
                        f"detections={len(trace)}" + (f" [{dets}]" if dets else "")
                        + f" -> {result} events={events}"
                        f" (min_conf={self._get_min_confidence():.2f} min_size={self._get_min_object_size():.4f})")
        except Exception:
            logger.exception(f"[Cam {self.cam.id}] debug log failed")

    def _should_detect_type(self, object_type: str) -> bool:
        """Check if any user wants to detect this object type."""
        field_name = f"motion_detect_{object_type}"
        return any(getattr(ps, field_name, 0) == 1 for ps in self.user_settings)

    def _should_detect_license_plate(self) -> bool:
        """Check if any user wants license plate OCR."""
        return any(ps.motion_license_plate_ocr == 1 for ps in self.user_settings)

    def _get_min_confidence(self) -> float:
        """Get minimum confidence from user settings."""
        confidences = [ps.motion_min_confidence for ps in self.user_settings]
        return min(confidences) if confidences else 0.6

    def _get_min_object_size(self) -> float:
        """Get minimum object size from user settings."""
        sizes = [ps.motion_min_object_size for ps in self.user_settings]
        return min(sizes) if sizes else 0.02

    def _check_cooldown(self, object_type: str, now: float | None = None) -> bool:
        """Check cooldown for notifications."""
        current_time = time.time() if now is None else now

        # Check each user's cooldown
        for ps in self.user_settings:
            if ps.motion_cooldown_seconds == 0:
                continue

            key = (ps.user_id, object_type)
            last_time = self.last_notification_time.get(key, 0)

            if current_time - last_time < ps.motion_cooldown_seconds:
                return False

        return True

    async def _save_and_notify(
        self,
        frame: np.ndarray,
        object_type: str,
        bbox: tuple[int, int, int, int],
        confidence: float,
        object_size: float,
        license_plate: Optional[str],
        frame_ts: float | None = None,
    ):
        """Save event to database and notify users."""
        try:
            # Save frame as JPEG
            if frame_ts is None:
                frame_ts = time.time()
            timestamp = int(frame_ts)
            filename = f"{timestamp}_{object_type}_{int(confidence * 100)}.jpg"

            # Create directory
            photo_dir = os.path.join(settings.motion_files_path, str(self.cam.id))
            os.makedirs(photo_dir, exist_ok=True)

            photo_path = os.path.join(photo_dir, filename)

            logger.debug(f"[Cam {self.cam.id}] Saving event: {object_type} conf={confidence:.2f} size={object_size:.4f}")
            logger.debug(f"[Cam {self.cam.id}] Photo path: {photo_path}")

            # Draw bounding box on frame
            x, y, w, h = bbox
            frame_copy = frame.copy()
            cv2.rectangle(frame_copy, (x, y), (x + w, y + h), (0, 255, 0), 2)

            # Save JPEG
            await asyncio.to_thread(cv2.imwrite, photo_path, frame_copy)
            logger.debug(f"[Cam {self.cam.id}] Photo saved successfully")

            # Save to database
            async with AsyncSessionLocal() as db:
                # Create motion event
                event = MotionEvent(
                    cam_id=self.cam.id,
                    detected_at=timestamp,
                    object_type=object_type,
                    confidence=confidence,
                    object_size=object_size,
                    license_plate=license_plate,
                    photo_filename=filename,
                    bbox_x=x,
                    bbox_y=y,
                    bbox_width=w,
                    bbox_height=h,
                    notified_user_ids=json.dumps([ps.user_id for ps in self.user_settings]),
                )
                db.add(event)
                await db.commit()
                logger.debug(f"[Cam {self.cam.id}] Event saved to database (id={event.id})")

                # Send notifications
                logger.debug(f"[Cam {self.cam.id}] Sending notifications to {len(self.user_settings)} user(s)...")
                await self._notify_users(db, event, photo_path)

                # Update cooldown
                current_time = frame_ts
                for ps in self.user_settings:
                    key = (ps.user_id, object_type)
                    self.last_notification_time[key] = current_time
                    logger.debug(f"[Cam {self.cam.id}] Updated cooldown for user_id={ps.user_id}, {object_type}")

        except Exception as exc:
            logger.error(f"[Cam {self.cam.id}] Error saving/notifying event: {exc}", exc_info=True)

    async def _notify_users(self, db, event: MotionEvent, photo_path: str):
        """Send Telegram notifications to users."""
        try:
            # Get emoji
            emoji_map = {'person': '🚶', 'dog': '🐕', 'cat': '🐈', 'car': '🚗'}
            emoji = emoji_map.get(event.object_type, '📷')

            # Format Russian object name
            obj_names = {'person': 'человек', 'dog': 'собака', 'cat': 'кошка', 'car': 'машина'}
            obj_name = obj_names.get(event.object_type, event.object_type)

            # Format time
            dt = datetime.fromtimestamp(event.detected_at)
            time_str = dt.strftime("%d.%m.%Y %H:%M:%S")

            # Build message
            message = f"{emoji} Обнаружен {obj_name} на камере \"{self.cam.name or self.cam.id}\"\n\n"
            message += f"Время: {time_str}\n"
            message += f"Уверенность: {int(event.confidence * 100)}%"

            if event.license_plate:
                message += f"\nНомер машины: {event.license_plate}"

            # Send to each user
            user_ids = json.loads(event.notified_user_ids) if event.notified_user_ids else []
            for user_id in user_ids:
                user = await db.get(User, user_id)
                if user:
                    await push.notify_user(user, message, photo_path=photo_path)

        except Exception as exc:
            logger.error(f"Error notifying users: {exc}")
