"""REAL ffmpeg, no network: a generated fMP4 (HEVC like the real camera when libx265 is
available, else H.264, + AAC 48k mono) goes through a real IngestHub into the real HLS
transcoder with the production flags; the playlist and segments are checked with ffprobe.

Earlier fake-only tests missed invalid ffmpeg options, hence this one."""
import asyncio
import json
import shutil
import subprocess
import time

import pytest

from app.core.config import settings
from app.models.cams import Cam
from app.services import hls_manager as hm
from app.services.ingest import lifecycle
from app.services.ingest.hub import IngestHub
from app.services.ingest.supervisor import IngestSupervisor
from tests.ingest.test_real_transcoder import FFMPEG, FFPROBE, _generate, _encoders

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available"),
]


@pytest.fixture(scope="module")
def sample():
    enc = _encoders()
    if "libx264" not in enc:
        pytest.skip("libx264 not available")
    codec = "libx265" if "libx265" in enc else "libx264"
    try:
        return codec, _generate(codec, 14)
    except AssertionError:
        if codec == "libx264":
            raise
        return "libx264", _generate("libx264", 14)


def _probe(path):
    res = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


async def test_real_hls_from_hub(sample, tmp_path, monkeypatch):
    codec, data = sample
    hub = IngestHub()
    sup = IngestSupervisor(hub=hub)
    sup.is_running = lambda cam_id: True  # the hub is fed by hand below
    monkeypatch.setattr(lifecycle, "_supervisor", sup)
    monkeypatch.setattr(settings, "INGEST_ENABLED", True)
    monkeypatch.setattr(settings, "FFMPEG_BINARY", FFMPEG)
    monkeypatch.setattr(settings, "INGEST_REGISTRY_DIR", str(tmp_path / "ingest"))
    monkeypatch.setattr(settings, "HLS_TEMP_DIR", str(tmp_path / "hls"))
    monkeypatch.setattr(settings, "HLS_IDLE_TIMEOUT_SECONDS", 30)
    hm._sessions.clear()
    hm._cam_locks.clear()
    hm._continuity.clear()
    hm._registry = None
    hm._guard = asyncio.Lock()

    cam_hub = hub.get(1)

    async def feed():
        # roughly live: pre-roll arrives at once (so a GOP is stored), the rest paced
        pos, step = 0, 32 * 1024
        while pos < len(data):
            cam_hub.feed(data[pos:pos + step])
            pos += step
            if pos > len(data) // 4:
                await asyncio.sleep(10.0 * step / len(data))  # ~10 s for the remaining 3/4

    feeder = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(cam_hub.online.wait(), 10)
        cam = Cam(id=1, addr="127.0.0.1", port=554, path="/x", username="u", password="p")
        t0 = time.monotonic()
        session = await hm.get_or_create_session(cam)  # returns once a segment is listed
        first_playlist = time.monotonic() - t0
        assert session.source == "hub"
        text = session.playlist_text()
        segs = [ln for ln in text.splitlines() if ln.endswith(".ts")]
        print(f"\nREAL ffmpeg HLS: input={codec} first_playlist(>=1 segment)={first_playlist:.2f}s")
        assert first_playlist < 3.0, first_playlist
        assert len(segs) >= 1
        await asyncio.sleep(6.0)  # let several more segments appear
        text = session.playlist_text()
        segs = [ln for ln in text.splitlines() if ln.endswith(".ts")]
        assert len(segs) >= 3, text
        assert text.startswith("#EXTM3U") and "#EXT-X-TARGETDURATION" in text and "#EXT-X-ENDLIST" not in text
        assert "%" not in text and len(set(segs)) == len(segs)  # distinct, URL-safe names

        # every listed segment is a valid h264(main, yuv420p)+aac MPEG-TS
        for name in segs[:3]:
            info = _probe(session.temp_dir / name)
            streams = {s["codec_type"]: s for s in info["streams"]}
            v, a = streams["video"], streams["audio"]
            assert v["codec_name"] == "h264" and v["profile"] == "Main" and v["pix_fmt"] == "yuv420p"
            assert (v["width"], v["height"]) == (1280, 720)
            assert a["codec_name"] == "aac" and a["sample_rate"] == "48000" and a["channels"] == 1
        # the segments decode cleanly (the live playlist itself never ends, so not via it)
        for name in segs[:3]:
            dec = subprocess.run([FFMPEG, "-v", "error", "-i", str(session.temp_dir / name), "-f", "null", "-"],
                                 capture_output=True, text=True, timeout=60)
            assert dec.returncode == 0 and dec.stderr.strip() == "", dec.stderr
        assert "Unrecognized option" not in session.last_stderr_line
        assert "Could not find ref" not in session.last_stderr_line
        assert session.last_stderr_line == "" or "error" not in session.last_stderr_line.lower()
        # the viewer goes away: stop, clean up, release the hub
        await hm.stop(1)
        assert not session.temp_dir.exists() and hub.subscriber_count(1) == 0
    finally:
        feeder.cancel()
        await hm.stop_all()
