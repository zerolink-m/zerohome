"""REAL ffmpeg/ffprobe, no network: a generated fMP4 (HEVC like the real camera when
libx265 is there, else H.264; + AAC) goes through a real IngestHub; size/fps come from the
init segment, the real decoder (production flags) turns it into yuv420p frames.

Earlier fake-only tests missed invalid ffmpeg options, hence this one."""
import asyncio
import shutil
import subprocess
import time

import pytest

from app.core.config import settings
from app.services.fmp4 import Fmp4Splitter
from app.services.ingest import lifecycle
from app.services.ingest.hub import IngestHub
from app.services.ingest.supervisor import IngestSupervisor
from app.services.webrtc_manager import probe as probe_mod
from app.services.webrtc_manager import process as procs
from app.services.webrtc_manager.track import RTSPVideoStreamTrack
from tests.ingest.test_real_transcoder import FFMPEG, FFPROBE, _encoders, _generate
from tests.ingest.conftest import alive, wait_until

pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available")

SECONDS = 12
W, H, FPS = 1280, 720, 13
FRAME = W * H * 3 // 2


def _codecs():
    enc = _encoders() if FFMPEG else ""
    out = []
    if "libx265" in enc:
        out.append("libx265")
    if "libx264" in enc:
        out.append("libx264")
    return out or ["libx264"]


@pytest.fixture(scope="module", params=_codecs())
def sample(request):
    if request.param not in _encoders():
        pytest.skip(f"{request.param} not available")
    data = _generate(request.param, SECONDS)
    sp = Fmp4Splitter()
    fragments = sp.feed(data)
    assert sp.init_segment and fragments
    return request.param, data, sp.init_segment, fragments


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
    procs._registry = None
    probe_mod._cache.clear()
    yield hub
    procs.force_kill_all()
    procs._registry = None


async def test_geometry_from_init_segment(sample):
    codec, _, init, fragments = sample
    probe_mod._cache.clear()
    t0 = time.monotonic()
    info = await probe_mod.probe_fmp4(init, fragments)
    ms = (time.monotonic() - t0) * 1000
    only_init = await probe_mod.probe_fmp4(init + b"", None)  # cached by init hash
    print(f"\nREAL ffprobe on init ({codec}): {info} in {ms:.0f} ms")
    assert (info.width, info.height) == (W, H)
    assert abs(info.fps - FPS) < 0.5, info
    assert only_init == info and ms < 2000
    # the init segment alone carries no frame rate: it is flagged and never cached
    probe_mod._cache.clear()
    bare = await probe_mod.probe_fmp4(init, None)
    assert (bare.width, bare.height) == (W, H) and not bare.fps_known and not probe_mod._cache


async def test_decoder_flags_yield_whole_frames(sample, real_env):
    """Production decoder args, whole clip on stdin: output is whole yuv420p frames,
    about SECONDS*FPS of them, with nothing wrong on stderr."""
    codec, data, init, fragments = sample
    track = RTSPVideoStreamTrack("rtsp://u:p@127.0.0.1/x", cam_id=1)
    track._set_geometry(await probe_mod.probe_fmp4(init, fragments))
    assert track._frame_size == FRAME
    res = await asyncio.to_thread(
        subprocess.run, track._decoder_args("hub"), input=data, capture_output=True, timeout=120
    )
    err = res.stderr.decode(errors="replace")
    frames = len(res.stdout) / FRAME
    print(f"\nREAL decoder ({codec}): {len(res.stdout)} bytes = {frames:.2f} frames (expected ~{SECONDS * FPS})")
    assert res.returncode == 0, err
    assert len(res.stdout) % FRAME == 0
    assert abs(frames - SECONDS * FPS) <= 3, frames
    assert "Unrecognized option" not in err and "Could not find ref" not in err and "Error" not in err, err


async def test_track_from_real_hub_first_frame_and_live_edge(sample, real_env):
    codec, _, init, fragments = sample
    hub = real_env
    cam_hub = hub.get(1)
    # Warm hub: init + a stored GOP, then the stream continues live (paced like a camera).
    cam_hub.feed(init)
    pre = 0
    for frag in fragments:
        cam_hub.feed(frag)
        pre += 1
        if cam_hub.gop_bytes > 20_000 and pre >= 3:
            break
    assert cam_hub.online.is_set()
    rest = fragments[pre:]

    async def live():
        for frag in rest:
            cam_hub.feed(frag)
            await asyncio.sleep(0.5)

    feeder = asyncio.create_task(live())
    ended = []
    track = RTSPVideoStreamTrack("rtsp://u:p@127.0.0.1/x", cam_id=1, on_end=ended.append)
    try:
        t0 = time.monotonic()
        first = await asyncio.wait_for(track.recv(), 30)
        first_ms = (time.monotonic() - t0) * 1000
        assert track.source == "hub" and (first.width, first.height) == (W, H) and first.format.name == "yuv420p"
        got = 1
        stamps = [time.monotonic()]
        while got < 20:
            await asyncio.wait_for(track.recv(), 15)
            got += 1
            stamps.append(time.monotonic())
        span = stamps[-1] - stamps[0]
        print(f"\nREAL track ({codec}): first_frame={first_ms:.0f} ms (incl. local ffprobe+decoder spawn), "
              f"20 frames in {span:.2f}s, dropped={track._dropped}, queue={len(track._frames)}, "
              f"max_queue_cap={int(FPS * settings.WEBRTC_MAX_LATENCY_MS / 1000)}")
        assert first_ms < 3000
        assert len(track._frames) <= int(FPS * 3.0)  # no growing backlog
        assert not ended
        pid = track.owned.pid
    finally:
        feeder.cancel()
        await track.aclose()
    assert not alive(pid) and hub.subscriber_count(1) == 0
    stderr = track._last_stderr_line
    assert "Unrecognized option" not in stderr and "Could not find ref" not in stderr, stderr
