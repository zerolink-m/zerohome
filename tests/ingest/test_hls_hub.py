"""hls_manager with the hub source (stage 3), on the fake ffmpeg (tests/fake_ffmpeg_hls.py).

Ingest is the real supervisor + hub (the fake in its ingest role); HLS ffmpeg is the same
script in its `-f hls` role: stdin (hub) or RTSP URL (direct)."""
import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
import pytest_asyncio

from app.core.config import settings
from app.models.cams import Cam
from app.services import hls_manager as hm
from app.services.ingest import lifecycle, ownership
from tests.ingest.conftest import alive, starts, target, wait_until

FAKE_HLS = str(Path(__file__).resolve().parent.parent / "fake_ffmpeg_hls.py")

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_bin(monkeypatch):  # overrides conftest: one binary serves ingest and HLS
    monkeypatch.setattr(settings, "FFMPEG_BINARY", FAKE_HLS)
    return FAKE_HLS


def cam(cam_id=1, path="/ok?rate=20&gop=5"):
    return Cam(id=cam_id, addr="127.0.0.1", port=554, path=path, username="user", password="secret")


@pytest.fixture
def args_log(tmp_path, monkeypatch):
    log = tmp_path / "args.log"
    monkeypatch.setenv("FAKE_FFMPEG_ARGS_LOG", str(log))
    return log


def hls_runs(log):
    runs = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
    return [r for r in runs if "hls" in r]


@pytest_asyncio.fixture
async def mgr(sup, fake_log, args_log, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "HLS_ENABLED", True)
    monkeypatch.setattr(settings, "HLS_TEMP_DIR", str(tmp_path / "hls"))
    monkeypatch.setattr(settings, "HLS_IDLE_TIMEOUT_SECONDS", 0.6)
    monkeypatch.setattr(settings, "HLS_START_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(settings, "HLS_STALL_SECONDS", 1.0)
    monkeypatch.setattr(settings, "INGEST_ENABLED", True)
    monkeypatch.setattr(settings, "INGEST_REGISTRY_DIR", str(tmp_path / "ingest"))
    monkeypatch.setattr(settings, "INGEST_HUB_WAIT_SECONDS", 0.6)
    monkeypatch.setattr(hm, "_TERM_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(hm, "_KILL_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(hm, "_MONITOR_TICK_SECONDS", 0.1)
    monkeypatch.setattr(lifecycle, "_supervisor", sup)
    hm._sessions.clear()
    hm._cam_locks.clear()
    hm._continuity.clear()
    hm._registry = None
    hm._guard = asyncio.Lock()
    await sup.start(target(1, "ok", rate=20, gop=5))
    assert await wait_until(lambda: sup.hub.get(1).online.is_set())
    yield hm
    await hm.stop_all()
    hm._sessions.clear()
    hm._registry = None
    for pid, _ in starts(fake_log):
        if alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass


def subscribers(cam_id=1):
    return lifecycle.get_supervisor().hub.subscriber_count(cam_id)


async def test_first_request_starts_ffmpeg_from_hub(mgr, args_log, sup):
    assert hls_runs(args_log) == [] and subscribers() == 0  # nothing runs without a viewer
    s = await hm.get_or_create_session(cam())
    assert s.source == "hub" and alive(s.pid) and subscribers() == 1
    run = hls_runs(args_log)
    assert len(run) == 1
    argv = run[0]
    assert argv[argv.index("-i") + 1] == "pipe:0"
    assert argv[argv.index("-c:v") + 1] == "libx264" and argv[argv.index("-c:a") + 1] == "copy"
    assert "-re" not in argv and "-fflags" not in argv
    assert argv[argv.index("-hls_time") + 1] == str(settings.HLS_SEGMENT_DURATION)
    assert argv[argv.index("-hls_list_size") + 1] == str(settings.HLS_PLAYLIST_SIZE)
    assert "secret" not in " ".join(argv)
    text = s.playlist_text()
    seg = [ln for ln in text.splitlines() if ln.endswith(".ts")][0]
    assert (s.temp_dir / seg).is_file() and s.active_marker.exists()
    assert seg.startswith("seg_0_") and "%" not in seg
    reg = json.loads((Path(settings.ingest_registry_path).with_name("hls_registry.json")).read_text())
    assert [e["pid"] for e in reg["entries"]] == [s.pid]
    await hm.stop(1)


async def test_concurrent_requests_share_one_ffmpeg(mgr, args_log):
    sessions = await asyncio.gather(*[hm.get_or_create_session(cam()) for _ in range(5)])
    assert len({id(s) for s in sessions}) == 1
    assert len(hls_runs(args_log)) == 1 and subscribers() == 1


async def test_idle_stops_ffmpeg_ingest_keeps_running(mgr, args_log, sup):
    s = await hm.get_or_create_session(cam())
    hm.schedule_idle_stop(1)
    assert await wait_until(lambda: not alive(s.pid), timeout=4.0)
    assert await wait_until(lambda: not s.temp_dir.exists())
    assert 1 not in hm._sessions and await wait_until(lambda: subscribers() == 0)
    assert sup.hub.get(1).online.is_set() and sup.is_running(1)
    s2 = await hm.get_or_create_session(cam())  # a returning viewer starts a new one
    assert s2 is not s and s2.source == "hub" and len(hls_runs(args_log)) == 2
    await hm.stop(1)


async def test_activity_postpones_idle_stop(mgr):
    s = await hm.get_or_create_session(cam())
    for _ in range(4):
        hm.schedule_idle_stop(1)
        await asyncio.sleep(0.3)
        assert (await hm.get_or_create_session(cam())) is s
    assert alive(s.pid)
    await hm.stop(1)


async def test_fallback_when_ingest_disabled(mgr, monkeypatch, args_log):
    monkeypatch.setattr(settings, "INGEST_ENABLED", False)
    s = await hm.get_or_create_session(cam())
    assert s.source == "direct" and subscribers() == 0
    argv = hls_runs(args_log)[0]
    assert argv[argv.index("-i") + 1].startswith("rtsp://user:secret@127.0.0.1")
    assert s.playlist_text()
    await hm.stop(1)


async def test_fallback_when_cam_not_in_ingest(mgr):
    s = await hm.get_or_create_session(cam(cam_id=7))
    assert s.source == "direct"
    await hm.stop(7)


async def test_fallback_when_hub_not_online_in_time(mgr, sup):
    await sup.start(target(3, "silent"))
    t0 = time.monotonic()
    s = await hm.get_or_create_session(cam(cam_id=3))
    assert s.source == "direct" and 0.5 <= time.monotonic() - t0 < 4.0
    await hm.stop(3)


@pytest.mark.parametrize("mode", ["reject", "silent"])
async def test_fallback_when_hub_ffmpeg_fails(mgr, monkeypatch, mode, fake_log):
    monkeypatch.setenv("FAKE_HLS_HUB_MODE", mode)
    monkeypatch.setattr(settings, "HLS_START_TIMEOUT_SECONDS", 1.0)
    s = await hm.get_or_create_session(cam())
    assert s.source == "direct" and alive(s.pid)
    assert await wait_until(lambda: subscribers() == 0)  # the failed attempt released the hub
    pids = [p for p, _ in starts(fake_log)]
    assert len([p for p in pids if alive(p)]) == 2  # ingest + the direct ffmpeg only
    await hm.stop(1)


async def test_both_sources_failing_raises_cleanly(mgr, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_HLS_HUB_MODE", "reject")
    monkeypatch.setenv("FAKE_HLS_DIRECT_MODE", "reject")
    with pytest.raises(hm.HLSStreamError) as exc:
        await hm.get_or_create_session(cam())
    assert "secret" not in str(exc.value) and "Unrecognized" in str(exc.value)
    assert 1 not in hm._sessions and not (tmp_path / "hls" / "1").exists()
    assert subscribers() == 0


async def test_ffmpeg_missing_is_an_error_not_a_hang(mgr, monkeypatch):
    monkeypatch.setattr(settings, "FFMPEG_BINARY", "/nonexistent/ffmpeg")
    monkeypatch.setattr(settings, "INGEST_ENABLED", False)
    with pytest.raises(hm.HLSStreamError):
        await hm.get_or_create_session(cam())


async def test_ingest_restart_ends_session_and_next_one_continues_numbering(mgr, sup, monkeypatch):
    monkeypatch.setattr(settings, "HLS_PLAYLIST_SIZE", 3)
    s = await hm.get_or_create_session(cam())
    assert await sup.restart(1)
    assert await wait_until(lambda: 1 not in hm._sessions and not alive(s.pid))
    assert await wait_until(lambda: not s.temp_dir.exists())
    assert await wait_until(lambda: sup.hub.get(1).online.is_set())
    s2 = await hm.get_or_create_session(cam())
    assert s2 is not s and s2.epoch == 1 and s2.start_seq > 0
    text = s2.playlist_text()
    assert text.count("#EXT-X-DISCONTINUITY\n") == 1
    assert f"#EXT-X-MEDIA-SEQUENCE:{s2.start_seq}" in text
    assert "DISCONTINUITY-SEQUENCE" not in text  # disc-seq 0 (epoch-1) is left out
    # once the tagged segment slid out of the window the sequence counts it instead
    assert await wait_until(lambda: f"#EXT-X-MEDIA-SEQUENCE:{s2.start_seq}\n" not in s2.playlist_text())
    later = s2.playlist_text()
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in later and "#EXT-X-DISCONTINUITY\n" not in later
    names = [ln for ln in later.splitlines() if ln.endswith(".ts")]
    assert all(n.startswith("seg_") and f"_{s2.segment_stamp}" in n for n in names)
    assert s2.segment_stamp != s.segment_stamp
    await hm.stop(1)


async def test_playlist_text_continuity_rendering(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "HLS_TEMP_DIR", str(tmp_path))
    s = hm._HLSSession(5, "")
    s.temp_dir.mkdir(parents=True)
    body = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:{n}\n" \
           "#EXTINF:2.0,\nseg_{n}_1.ts\n#EXTINF:2.0,\nseg_{m}_1.ts\n"
    s.epoch, s.start_seq = 0, 0
    s.playlist_path.write_text(body.format(n=0, m=1))
    assert s.playlist_text() == body.format(n=0, m=1)  # first run: untouched
    s.epoch, s.start_seq = 3, 40
    s.playlist_path.write_text(body.format(n=40, m=41))
    t = s.playlist_text()
    assert t.splitlines()[3:6] == ["#EXT-X-MEDIA-SEQUENCE:40", "#EXT-X-DISCONTINUITY-SEQUENCE:2", "#EXT-X-DISCONTINUITY"]
    assert t.index("#EXT-X-DISCONTINUITY\n") < t.index("#EXTINF")
    s.playlist_path.write_text(body.format(n=41, m=42))
    t = s.playlist_text()
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:3" in t and "#EXT-X-DISCONTINUITY\n" not in t
    assert not s.playlist_path.with_name("nope").exists() and hm._HLSSession(6, "").playlist_text() == ""


async def test_overflow_of_feed_ends_session(mgr):
    s = await hm.get_or_create_session(cam())
    s.sub._finish("overflow")  # the hub cut the subscription off
    assert await wait_until(lambda: not alive(s.pid) and 1 not in hm._sessions)
    assert "overflow" in s.end_reason
    s2 = await hm.get_or_create_session(cam())
    assert s2 is not s
    await hm.stop(1)


async def test_ffmpeg_crash_ends_session_and_masks_password(mgr, monkeypatch, caplog):
    monkeypatch.setenv("FAKE_HLS_HUB_MODE", "crash")
    monkeypatch.setattr(settings, "HLS_START_TIMEOUT_SECONDS", 2.0)
    s = await hm.get_or_create_session(cam())  # two segments appear before it dies
    assert await wait_until(lambda: 1 not in hm._sessions)
    assert "secret" not in s.last_stderr_line and "boom" in s.last_stderr_line
    assert not any("secret" in r.getMessage() for r in caplog.records)
    assert await wait_until(lambda: not s.temp_dir.exists())


async def test_stalled_ffmpeg_is_killed_by_watchdog(mgr, monkeypatch):
    monkeypatch.setenv("FAKE_HLS_HUB_MODE", "stall")
    monkeypatch.setattr(settings, "HLS_STALL_SECONDS", 0.8)
    s = await hm.get_or_create_session(cam())
    assert alive(s.pid)
    assert await wait_until(lambda: not alive(s.pid) and 1 not in hm._sessions, timeout=5.0)
    assert s.end_reason == "ffmpeg stalled" and await wait_until(lambda: subscribers() == 0)


async def test_stop_all_bounded_with_sigterm_ignoring_ffmpeg(mgr, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_HLS_HUB_MODE", "hang")
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_FFMPEG_CHILD_PIDFILE", str(pidfile))
    s = await hm.get_or_create_session(cam())
    t0 = time.monotonic()
    await hm.stop_all()
    assert time.monotonic() - t0 < 4.0
    assert await wait_until(lambda: not alive(s.pid), timeout=2.0)
    assert await wait_until(lambda: not alive(int(pidfile.read_text())), timeout=2.0)  # its child too
    assert not s.temp_dir.exists() and subscribers() == 0


async def test_stop_by_cam_id(mgr):
    s = await hm.get_or_create_session(cam())
    await hm.stop(1)
    assert not alive(s.pid) and not s.temp_dir.exists()
    await hm.stop(1)  # nothing to stop: harmless


async def test_no_process_leak(mgr, sup, fake_log):
    for _ in range(3):
        await hm.get_or_create_session(cam())
        await hm.stop(1)
    await hm.stop_all()
    await sup.stop_all(timeout=4.0)
    pids = [p for p, _ in starts(fake_log)]
    assert len(pids) >= 4
    assert await wait_until(lambda: not any(alive(p) for p in pids))
    reg = json.loads(hm._registry_path().read_text())
    assert reg["entries"] == []


async def test_startup_reaps_only_verified_orphans(mgr, monkeypatch):
    inst = "deadbeefdeadbeef"
    env = {**os.environ, ownership.ENV_TAG: ownership.make_tag(inst, 5)}
    cmd = [FAKE_HLS, "-i", "pipe:0", "-f", "hls", "/nonexistent/x.m3u8"]
    quiet = {"FAKE_HLS_HUB_MODE": "silent"}
    orphan = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, env={**env, **quiet},
                              start_new_session=True)
    stranger = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, env={**os.environ, **quiet},
                                start_new_session=True)
    try:
        await asyncio.sleep(0.3)
        path = hm._registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"instance_id": inst, "entries": [
            {"cam_id": 5, "pid": orphan.pid, "start_time": ownership.read_start_time(orphan.pid)},
            {"cam_id": 6, "pid": stranger.pid, "start_time": ownership.read_start_time(stranger.pid)},
        ]}))
        hm._registry = None
        await hm.startup()
        assert await wait_until(lambda: orphan.poll() is not None, timeout=5.0)
        assert stranger.poll() is None and alive(stranger.pid)
        assert ownership.Registry.load_previous(path)[1] == []
    finally:
        for p in (orphan, stranger):
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()
