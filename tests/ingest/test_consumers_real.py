"""REAL ffmpeg/ffprobe, no network: a generated fMP4 (HEVC like the real camera when libx265
is there, else H.264; 1280x720 @ 13 fps, GOP 40 frames ~ 3 s) goes through a real IngestHub
into the stage 5 consumers with their production flags: the live snapshot (init + last GOP ->
JPEG) and the motion detection frame source (bgr24 frames at 1 fps).

The picture gets brighter with time, so "which frame" can be told from the brightness.
Earlier fake-only tests missed invalid ffmpeg options, hence this one."""
import asyncio
import subprocess
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from app.core.config import settings
from app.models.cams import Cam
from app.services import snapshot
from app.services.fmp4 import Fmp4Splitter
from app.services.ingest import lifecycle, ownership
from app.services.ingest.hub import IngestHub
from app.services.ingest.supervisor import IngestSupervisor
from app.services.motion_detection import session as md_session
from app.services.motion_detection.session import MotionDetectionSession
from app.services.webrtc_manager import probe as probe_mod
from tests.ingest.conftest import alive, wait_until
from tests.ingest.test_real_transcoder import FFMPEG, FFPROBE, _encoders

pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available")

SECONDS = 15
W, H, FPS, GOP = 1280, 720, 13, 40


def _generate(codec: str) -> bytes:
    vopts = {
        "libx265": ["-c:v", "libx265", "-x265-params", f"log-level=error:keyint={GOP}:min-keyint={GOP}:scenecut=0:open-gop=0"],
        "libx264": ["-c:v", "libx264", "-g", str(GOP), "-keyint_min", str(GOP), "-sc_threshold", "0"],
    }[codec]
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=gray:size={W}x{H}:rate={FPS},geq=lum='min(235,16+T*12)':cb=128:cr=128",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(SECONDS), *vopts, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "1", "-b:a", "64k",
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1",
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=180)
    assert res.returncode == 0, res.stderr.decode(errors="replace")
    return res.stdout


@pytest.fixture(scope="module", params=["libx265", "libx264"])
def sample(request):
    if request.param not in _encoders():
        pytest.skip(f"{request.param} not available")
    data = _generate(request.param)
    sp = Fmp4Splitter()
    fragments = sp.feed(data)
    assert sp.init_segment and len(fragments) >= 4
    return request.param, sp.init_segment, fragments


def _gop_luma(init: bytes, fragment: bytes) -> list[float]:
    """Mean luma of every frame of one fragment (reference decode)."""
    res = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-map", "0:v:0", "-f", "rawvideo",
         "-pix_fmt", "gray", "pipe:1"], input=init + fragment, capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr
    frames = np.frombuffer(res.stdout, dtype=np.uint8).reshape(-1, H, W)
    return [float(f.mean()) for f in frames]


@pytest.fixture
def real_env(tmp_path, monkeypatch):
    hub = IngestHub()
    sup = IngestSupervisor(hub=hub)
    sup.is_running = lambda cam_id: True  # the hub is fed by hand
    monkeypatch.setattr(lifecycle, "_supervisor", sup)
    monkeypatch.setattr(settings, "INGEST_ENABLED", True)
    monkeypatch.setattr(settings, "FFMPEG_BINARY", FFMPEG)
    monkeypatch.setattr(settings, "FFPROBE_BINARY", FFPROBE)
    monkeypatch.setattr(settings, "INGEST_REGISTRY_DIR", str(tmp_path / "ingest"))
    monkeypatch.setattr(md_session, "_get_yolo_model", lambda: object())
    for pool in (snapshot._pool, md_session.pool):
        pool.registry = None
        pool.live.clear()
    snapshot._hub_last.clear()
    snapshot._hub_locks.clear()
    probe_mod._cache.clear()
    yield hub
    snapshot._pool.force_kill_all()
    md_session.pool.force_kill_all()
    snapshot._pool.registry = md_session.pool.registry = None


async def test_snapshot_from_init_and_last_gop_is_a_valid_fresh_jpeg(sample, real_env):
    codec, init, fragments = sample
    cam_hub = real_env.get(1)
    cam_hub.feed(init)
    for frag in fragments[:3]:
        cam_hub.feed(frag)
    assert len(cam_hub.gop) == 1  # one GOP = one fragment
    cam = Cam(id=1, addr="127.0.0.1", port=554, path="/x", username="u", password="p")

    t0 = time.monotonic()
    data = await snapshot._grab_from_hub(cam)
    first_ms = (time.monotonic() - t0) * 1000
    assert data is not None
    # a second request for the same GOP is served from the per-camera cache
    t0 = time.monotonic()
    assert await snapshot._grab_from_hub(cam) == data
    cached_ms = (time.monotonic() - t0) * 1000
    # a fresh decode (new GOP) for a timing sample without cache
    cam_hub.feed(fragments[3])
    t0 = time.monotonic()
    data2 = await snapshot._grab_from_hub(cam)
    second_ms = (time.monotonic() - t0) * 1000
    print(f"\nREAL snapshot from hub ({codec}): {len(data)} bytes, decode {first_ms:.0f} ms / {second_ms:.0f} ms, "
          f"same-GOP cache {cached_ms:.1f} ms")

    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and img.shape == (H, W, 3)
    assert data[:3] == b"\xff\xd8\xff" and data.endswith(b"\xff\xd9")
    # the NEWEST frame of the GOP, not its keyframe: brightness grows with time
    luma = _gop_luma(init, fragments[2])
    mean = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean())
    assert abs(mean - luma[-1]) < abs(mean - luma[0]), (mean, luma[0], luma[-1])
    assert abs(mean - luma[-1]) < 6, (mean, luma[-1])
    assert cv2.imdecode(np.frombuffer(data2, dtype=np.uint8), cv2.IMREAD_COLOR) is not None
    assert first_ms < 3000 and cached_ms < 50
    # nothing left behind
    assert await wait_until(lambda: not snapshot._pool.live)
    assert ownership.Registry.load_previous(snapshot._pool.registry_path())[1] == []


async def test_motion_source_decodes_native_size_frames_from_live_fragments_only(sample, real_env):
    codec, init, fragments = sample
    cam_hub = real_env.get(1)
    cam_hub.feed(init)
    cam_hub.feed(fragments[0])  # stored GOP: stale, must not reach the detector
    stale_last = _gop_luma(init, fragments[0])[-1]

    ps = SimpleNamespace(user_id=1, motion_detect_person=1, motion_detect_dog=1, motion_detect_cat=1,
                         motion_detect_car=1, motion_min_confidence=0.5, motion_min_object_size=0.01,
                         motion_license_plate_ocr=0)
    s = MotionDetectionSession(Cam(id=1, name="c", proto="rtsp", addr="127.0.0.1", port=554, path="/x",
                                   username="u", password="p"), [ps])
    seen = []

    async def process(frame, model, n, ts=None):
        seen.append((n, ts, frame.shape, float(frame.mean()), time.time()))

    s._process_frame = process
    t0 = time.monotonic()
    await s.start()
    assert await wait_until(lambda: real_env.subscriber_count(1) == 1 and s.source == "hub", timeout=10)
    cam_hub.feed(fragments[1])
    assert await wait_until(lambda: len(seen) >= 2, timeout=15)
    first_ms = (time.monotonic() - t0) * 1000
    cam_hub.feed(fragments[2])
    assert await wait_until(lambda: len(seen) >= 5, timeout=15)
    print(f"\nREAL motion source ({codec}): first frame {first_ms:.0f} ms after start, "
          f"{len(seen)} frames, shapes {sorted({x[2] for x in seen})}")
    assert {x[2] for x in seen} == {(H, W, 3)}
    # fps=1 on a 3.07 s fragment: 3 frames; fragments[1] and [2] -> about 6, never the stale GOP's
    assert 5 <= len(seen) <= 8
    lums = [x[3] for x in seen]
    # the BGR mean differs from the gray mean by a few levels; stale frames would be ~35 darker
    assert min(lums) > stale_last - 8, (lums, stale_last)
    assert lums == sorted(lums) or max(a - b for a, b in zip(lums, lums[1:])) < 4
    ts = [x[1] for x in seen]
    assert all(b > a for a, b in zip(ts, ts[1:]))
    assert all(abs(x[4] - x[1]) < 4.0 for x in seen)  # estimated capture time is close to arrival

    pids = [o.pid for o in md_session.pool.live]
    assert pids
    await s.stop()
    assert not any(alive(p) for p in pids) and real_env.subscriber_count(1) == 0
    assert not md_session.pool.live
    assert ownership.Registry.load_previous(md_session.pool.registry_path())[1] == []
