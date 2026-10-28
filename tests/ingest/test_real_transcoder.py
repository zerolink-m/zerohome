"""REAL ffmpeg, no network: a generated fMP4 (HEVC like the real camera when libx265 is
available, else H.264, + AAC 48k mono) goes through a real IngestHub into the real
transcoder with the production flags; the output is checked with ffprobe/ffmpeg.

Earlier fake-only tests missed invalid ffmpeg options, hence this one."""
import asyncio
import json
import shutil
import subprocess
import time

import pytest

from app.core.config import settings
from app.models.cams import Cam
from app.services import stream_manager as sm
from app.services.ingest import lifecycle
from app.services.ingest.hub import IngestHub
from app.services.ingest.supervisor import IngestSupervisor

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available")


def _encoders() -> str:
    return subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


def _generate(codec: str, seconds: int = 12) -> bytes:
    vopts = {
        "libx265": ["-c:v", "libx265", "-x265-params", "log-level=error:keyint=40:min-keyint=40:scenecut=0:open-gop=0"],
        "libx264": ["-c:v", "libx264", "-g", "40", "-keyint_min", "40", "-sc_threshold", "0"],
    }[codec]
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=13",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(seconds), *vopts, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "1", "-b:a", "64k",
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1",
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=120)
    assert res.returncode == 0, res.stderr.decode(errors="replace")
    return res.stdout


@pytest.fixture(scope="module")
def sample():
    enc = _encoders()
    if "libx264" not in enc:
        pytest.skip("libx264 not available")
    codec = "libx265" if "libx265" in enc else "libx264"
    try:
        return codec, _generate(codec)
    except AssertionError:
        if codec == "libx264":
            raise
        return "libx264", _generate("libx264")


async def test_real_transcoder_from_hub(sample, tmp_path, monkeypatch):
    codec, data = sample
    hub = IngestHub()
    sup = IngestSupervisor(hub=hub)
    sup.is_running = lambda cam_id: True  # the hub is fed by hand below
    monkeypatch.setattr(lifecycle, "_supervisor", sup)
    monkeypatch.setattr(settings, "INGEST_ENABLED", True)
    monkeypatch.setattr(settings, "FFMPEG_BINARY", FFMPEG)
    monkeypatch.setattr(settings, "INGEST_REGISTRY_DIR", str(tmp_path / "ingest"))
    monkeypatch.setattr(settings, "INGEST_IDLE_TRANSCODE_LINGER_SECONDS", 1.0)
    sm._sessions.clear()
    sm._cam_locks.clear()
    sm._registry = None
    sm._guard = asyncio.Lock()

    cam_hub = hub.get(1)
    done = asyncio.Event()

    async def feed():
        # roughly live: pre-roll arrives at once (so a GOP is stored), the rest paced
        pos, step = 0, 32 * 1024
        while pos < len(data):
            cam_hub.feed(data[pos:pos + step])
            pos += step
            if pos > len(data) // 4:
                await asyncio.sleep(8.0 * step / len(data))  # ~8 s for the remaining 3/4
        done.set()

    feeder = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(cam_hub.online.wait(), 10)
        cam = Cam(id=1, addr="127.0.0.1", port=554, path="/x", username="u", password="p")
        t0 = time.monotonic()
        queue, session = await sm.get_or_attach(cam)  # returns once the init segment is out
        first_byte = time.monotonic() - t0
        assert session.source == "hub"

        chunks, t_media = [], None
        async def collect():
            nonlocal t_media
            async for chunk in sm.iter_queue(queue):
                chunks.append(chunk)
                if t_media is None and len(chunks) == 2:
                    t_media = time.monotonic() - t0
                if len(chunks) >= 5:
                    return
        await asyncio.wait_for(collect(), 30)
        print(f"\nREAL ffmpeg transcoder: input={codec} first_byte(init)={first_byte:.2f}s "
              f"first_media_fragment={t_media:.2f}s chunks={len(chunks)}")
        assert first_byte < 3.0, first_byte
        assert t_media is not None and t_media < 3.0, t_media  # the replayed GOP is not lost
        assert len(chunks) >= 3
        await sm.detach(1, session, queue)
        await sm.stop_all()
    finally:
        feeder.cancel()
        await sm.stop_all()

    out = tmp_path / "out.mp4"
    out.write_bytes(b"".join(chunks))
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(out)],
        capture_output=True, text=True, timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    streams = {s["codec_type"]: s for s in json.loads(probe.stdout)["streams"]}
    v = streams["video"]
    assert v["codec_name"] == "h264" and v["profile"] == "Main" and v["pix_fmt"] == "yuv420p"
    assert (v["width"], v["height"]) == (1280, 720)
    a = streams["audio"]
    assert a["codec_name"] == "aac" and a["sample_rate"] == "48000" and a["channels"] == 1
    # decodes cleanly end to end
    dec = subprocess.run([FFMPEG, "-v", "error", "-i", str(out), "-f", "null", "-"],
                         capture_output=True, text=True, timeout=60)
    assert dec.returncode == 0 and dec.stderr.strip() == "", dec.stderr
    # the transcoder itself reported nothing wrong (no unknown options, no decoder errors)
    assert "Unrecognized option" not in session.last_stderr_line
    assert "Could not find ref" not in session.last_stderr_line
    # it is a fragmented mp4 starting with ftyp+moov, then moof boxes
    assert chunks[0][4:8] == b"ftyp" and b"moov" in chunks[0][:4096]
    assert all(c[4:8] == b"moof" for c in chunks[1:])
