"""WebRTC video track on the hub source (stage 4), on the fake ffmpeg/ffprobe
(tests/fake_ffmpeg_webrtc.py). Real ffmpeg: test_webrtc_real.py."""
import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

import pytest
import pytest_asyncio
from aiortc.mediastreams import MediaStreamError

from app.core.config import settings
from app.models.cams import Cam
from app.services.ingest import lifecycle, ownership
from app.services.ingest.hub import IngestHub
from app.services.ingest.supervisor import IngestSupervisor
from app.services.webrtc_manager import probe as probe_mod
from app.services.webrtc_manager import process as procs
from app.services.webrtc_manager.ice import WebRTCStreamError
from app.services.webrtc_manager.track import RTSPVideoStreamTrack
from tests.fmp4_builder import fragment, init_segment
from tests.ingest.conftest import alive, wait_until

FAKE = str(Path(__file__).resolve().parent.parent / "fake_ffmpeg_webrtc.py")
URL = "rtsp://user:secret@127.0.0.1:554/x"


def pids(log: Path) -> list[int]:
    return [int(ln.split()[0]) for ln in log.read_text().splitlines()] if log.exists() else []


def runs(log: Path) -> list[list[str]]:
    return [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []


def decoder_runs(log: Path) -> list[list[str]]:
    return [a for a in runs(log) if "-show_entries" not in a]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FFMPEG_ARGS_LOG", str(tmp_path / "args.log"))
    monkeypatch.setenv("FAKE_FFMPEG_LOG", str(tmp_path / "pids.log"))
    monkeypatch.setenv("FAKE_WEBRTC_SIZE", "64x48@10")
    monkeypatch.setattr(settings, "FFMPEG_BINARY", FAKE)
    monkeypatch.setattr(settings, "FFPROBE_BINARY", FAKE)
    monkeypatch.setattr(settings, "INGEST_ENABLED", True)
    monkeypatch.setattr(settings, "INGEST_REGISTRY_DIR", str(tmp_path / "ingest"))
    monkeypatch.setattr(settings, "INGEST_HUB_WAIT_SECONDS", 0.4)
    monkeypatch.setattr(settings, "WEBRTC_FIRST_FRAME_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(settings, "WEBRTC_STALL_SECONDS", 1.0)
    monkeypatch.setattr(procs, "TERM_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(procs, "KILL_TIMEOUT_SECONDS", 1.0)
    procs._registry = None
    procs._live.clear()
    probe_mod._cache.clear()
    hub = IngestHub()
    sup = IngestSupervisor(hub=hub)
    running = {1: True}
    sup.is_running = lambda cam_id: running.get(cam_id, False)
    monkeypatch.setattr(lifecycle, "_supervisor", sup)
    e = type("Env", (), {})()
    e.hub, e.running, e.args_log, e.pid_log, e.tmp, e.mp = hub, running, tmp_path / "args.log", tmp_path / "pids.log", tmp_path, monkeypatch
    e.feed_init = lambda cam_id=1: hub.get(cam_id).feed(init_segment() + fragment(0) + fragment(1, key=False))
    yield e
    procs.force_kill_all()
    procs._registry = None
    for pid in pids(e.pid_log):
        if alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass


def make_track(**kw):
    ended = []
    track = RTSPVideoStreamTrack(URL, cam_id=1, on_end=ended.append, **kw)
    return track, ended


async def test_hub_source_decodes_from_stdin_without_touching_the_camera(env, caplog):
    caplog.set_level(logging.INFO)
    env.feed_init()
    track, ended = make_track()
    frame = await asyncio.wait_for(track.recv(), 5)
    assert track.source == "hub" and (frame.width, frame.height) == (64, 48)
    argvs = runs(env.args_log)
    assert len(argvs) == 2  # local ffprobe + decoder, nothing else
    assert not any(a.startswith("rtsp") for argv in argvs for a in argv)
    probe_argv, dec = argvs
    assert "pipe:0" in probe_argv and "-show_entries" in probe_argv
    assert dec[dec.index("-i") + 1] == "pipe:0" and "-re" not in dec and "nobuffer" not in " ".join(dec)
    assert dec[dec.index("-r") + 1] == "10" and "rawvideo" in dec and "yuv420p" in dec and "-an" in dec
    assert "scale" not in " ".join(dec)
    assert "webrtc: cam 1 source=hub first_frame_ms=" in caplog.text
    assert env.hub.subscriber_count(1) == 1
    # registered with start time and tagged, so a crashed app's decoder can be reaped
    entry = ownership.Registry.load_previous(procs.registry_path())[1][0]
    assert entry.pid == track.owned.pid and procs.registry_path().name == "webrtc_registry.json"
    assert ownership.read_environ(entry.pid)[ownership.ENV_TAG].endswith(f":{entry.cam_id}")
    pid = track.owned.pid
    await track.aclose()
    assert not alive(pid) and env.hub.subscriber_count(1) == 0
    assert ownership.Registry.load_previous(procs.registry_path())[1] == []
    assert ended == []  # an intentional stop is not an "end"


async def test_geometry_comes_from_the_probe_and_is_cached(env, monkeypatch):
    monkeypatch.setenv("FAKE_WEBRTC_SIZE", "32x24@5")
    env.feed_init()
    for _ in range(2):
        track, _ = make_track()
        frame = await asyncio.wait_for(track.recv(), 5)
        assert (frame.width, frame.height) == (32, 24)
        await track.aclose()
    probes = [a for a in runs(env.args_log) if "-show_entries" in a]
    assert len(probes) == 1  # second session: cached by init hash
    assert [a[a.index("-r") + 1] for a in decoder_runs(env.args_log)] == ["5", "5"]


async def test_replayed_gop_is_paced_and_stale_frames_are_dropped(env, monkeypatch):
    monkeypatch.setenv("FAKE_WEBRTC_BURST", "60")
    monkeypatch.setenv("FAKE_WEBRTC_SIZE", "16x16@10")
    monkeypatch.setattr(settings, "WEBRTC_MAX_LATENCY_MS", 500)  # 5 frames at 10 fps
    env.feed_init()
    track, _ = make_track()
    t0 = time.monotonic()
    first = await asyncio.wait_for(track.recv(), 5)
    assert time.monotonic() - t0 < 2.0
    await asyncio.sleep(0.3)  # the whole burst has been read by now
    frames = [first]
    stamps = [time.monotonic()]
    for _ in range(4):
        frames.append(await asyncio.wait_for(track.recv(), 5))
        stamps.append(time.monotonic())
    # not a fast-forward: at most ~fps frames per second leave the track
    assert stamps[-1] - stamps[1] >= 0.3
    pts = [f.pts for f in frames]
    assert pts == sorted(pts) and len(set(pts)) == len(pts)
    assert pts[-1] - pts[1] >= 0.3 * 90000 * 0.8  # timestamps follow real time, not frame count
    # A 6 s burst (one GOP) is kept smooth up to the 3 s reader cap, not cut to 0.5 s (that
    # would freeze the picture until the next fragment); the older half is dropped.
    assert track._dropped >= 30
    assert len(track._frames) <= 30
    seq = [bytes(f.planes[0])[0] for f in frames]
    assert seq[1] >= 30, seq
    await track.aclose()


async def test_no_hub_paths_fall_back_to_direct(env, monkeypatch):
    # ingest disabled
    monkeypatch.setattr(settings, "INGEST_ENABLED", False)
    env.feed_init()
    track, _ = make_track()
    await asyncio.wait_for(track.recv(), 5)
    assert track.source == "direct"
    await track.aclose()
    dec = decoder_runs(env.args_log)[-1]
    assert dec[dec.index("-i") + 1] == URL and "-rtsp_transport" in dec
    monkeypatch.setattr(settings, "INGEST_ENABLED", True)
    # camera not in ingest
    env.running[1] = False
    track, _ = make_track()
    await asyncio.wait_for(track.recv(), 5)
    assert track.source == "direct"
    await track.aclose()
    # ingest on, but the hub never gets online within INGEST_HUB_WAIT_SECONDS
    env.running[1] = True
    env.hub.get(1).reset_stream("restart")
    t0 = time.monotonic()
    track, _ = make_track()
    await asyncio.wait_for(track.recv(), 5)
    assert track.source == "direct" and time.monotonic() - t0 < 2.0
    await track.aclose()


async def test_probe_failure_or_rejecting_decoder_falls_back(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    env.feed_init()
    monkeypatch.setenv("FAKE_WEBRTC_PROBE", "fail")  # also fails direct probing -> use direct sizes below
    track, ended = make_track()
    with pytest.raises(MediaStreamError) as error:
        await asyncio.wait_for(track.recv(), 5)  # hub probe failed, direct probe failed too
    assert isinstance(error.value.__cause__, WebRTCStreamError)
    assert "ffprobe" in str(error.value.__cause__)
    assert "ffprobe" in track.end_reason
    assert ended == [track]
    assert "falling back to direct RTSP" in caplog.text
    assert "secret" not in caplog.text and "secret" not in track.end_reason
    probes = [a for a in runs(env.args_log) if "-show_entries" in a]
    assert len(probes) == 2 and "pipe:0" in probes[0] and URL in probes[1]
    assert env.hub.subscriber_count(1) == 0
    await track.aclose()
    monkeypatch.delenv("FAKE_WEBRTC_PROBE")
    # decoder refuses its options (rejects hub AND direct): no frames, clean error, nothing left
    monkeypatch.setenv("FAKE_WEBRTC_MODE", "reject")
    track, ended = make_track()
    with pytest.raises(MediaStreamError):
        await asyncio.wait_for(track.recv(), 8)
    assert track.source == "direct" and "secret" not in (track.end_reason or "")
    assert env.hub.subscriber_count(1) == 0 and not procs._live or await wait_until(lambda: not procs._live)
    assert len(ended) == 1


@pytest.mark.parametrize("hub_unavailable", ["offline", "probe_failed"])
async def test_cluster_does_not_fall_back_to_camera(env, monkeypatch, hub_unavailable):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)
    if hub_unavailable == "offline":
        env.running[1] = False
    else:
        env.feed_init()
        monkeypatch.setenv("FAKE_WEBRTC_PROBE", "fail")

    track, ended = make_track()
    with pytest.raises(MediaStreamError) as error:
        await asyncio.wait_for(track.recv(), 5)
    assert isinstance(error.value.__cause__, WebRTCStreamError)
    assert "ingest" in str(error.value.__cause__)
    assert "ingest" in track.end_reason and ended == [track]
    assert not any(URL in argv for argv in runs(env.args_log))
    assert env.hub.subscriber_count(1) == 0
    await track.aclose()


async def test_hub_decoder_failure_falls_back_to_direct_that_works(env, monkeypatch):
    env.feed_init()
    orig = RTSPVideoStreamTrack._decoder_args

    def args(self, source):
        a = orig(self, source)
        if source == "hub":
            a.insert(1, "--reject-me")
        return a

    # decoder in hub mode gets killed by the fake for this flag
    monkeypatch.setattr(RTSPVideoStreamTrack, "_decoder_args", args)
    real_spawn = procs.spawn

    async def spawn(a, **kw):
        if "--reject-me" in a:
            a = [x for x in a if x != "--reject-me"]
            monkeypatch.setenv("FAKE_WEBRTC_MODE", "reject")
            try:
                return await real_spawn(a, **kw)
            finally:
                monkeypatch.delenv("FAKE_WEBRTC_MODE")
        return await real_spawn(a, **kw)

    monkeypatch.setattr(procs, "spawn", spawn)
    track, ended = make_track()
    await asyncio.wait_for(track.recv(), 8)
    assert track.source == "direct" and ended == []
    await track.aclose()
    assert env.hub.subscriber_count(1) == 0


@pytest.mark.parametrize("how", ["restart", "stopped"])
async def test_hub_restart_or_stop_ends_session_for_reconnect(env, how):
    env.feed_init()
    track, ended = make_track()
    await asyncio.wait_for(track.recv(), 5)
    pid = track.owned.pid
    env.hub.get(1).reset_stream(how)
    assert await wait_until(lambda: ended)
    assert ended == [track] and track.reconnect and track.end_reason == f"ingest {how}"
    assert await wait_until(lambda: not alive(pid))  # decoder gone, not left on a dead timeline
    with pytest.raises(MediaStreamError):
        for _ in range(100):
            await asyncio.wait_for(track.recv(), 2)  # queued frames drain, then the end
    # a new session starts on the new init segment
    env.feed_init()
    track2, _ = make_track()
    assert (await asyncio.wait_for(track2.recv(), 5)).width == 64
    await track2.aclose()


async def test_subscriber_overflow_ends_session(env):
    env.feed_init()
    track, ended = make_track()
    await asyncio.wait_for(track.recv(), 5)
    track.sub._limit = 2_000
    env.hub.get(1).feed(fragment(5, key=True, size=3_000))  # decoder is "slow": over the limit
    assert await wait_until(lambda: ended)
    assert track.end_reason == "ingest overflow" and track.reconnect


async def test_decoder_crash_ends_session_and_masks_password(env, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("FAKE_WEBRTC_MODE", "crash")
    env.feed_init()
    track, ended = make_track()
    await asyncio.wait_for(track.recv(), 5)
    assert await wait_until(lambda: ended)
    assert not track.reconnect and track.end_reason.startswith("decoder ended")
    assert "boom" in track.end_reason and "secret" not in track.end_reason
    assert "secret" not in caplog.text


async def test_stalled_decoder_is_killed_by_watchdog(env, monkeypatch):
    monkeypatch.setenv("FAKE_WEBRTC_MODE", "hang")  # 3 frames, then silence, ignores SIGTERM
    env.feed_init()
    track, ended = make_track()
    await asyncio.wait_for(track.recv(), 5)
    pid = track.owned.pid
    assert await wait_until(lambda: ended, timeout=5)
    assert track.end_reason == "no frames from decoder"
    assert await wait_until(lambda: not alive(pid), timeout=5)  # SIGTERM ignored -> SIGKILL
    assert env.hub.subscriber_count(1) == 0


async def test_session_close_and_shutdown_leave_no_processes(env, monkeypatch):
    from app.services import webrtc_manager as wm

    monkeypatch.setattr(wm.session, "_build_rtsp_url", lambda cam: URL)
    monkeypatch.setenv("FAKE_WEBRTC_MODE", "hang")
    env.feed_init()
    cam = Cam(id=1, addr="127.0.0.1", port=554, path="/x", username="user", password="secret")
    ended = []
    sids = []
    for i in range(3):
        sid = f"s{i}"
        await wm.create_session(cam, sid, on_end=ended.append)
        await asyncio.wait_for(wm.session._sessions[sid].video_track.recv(), 5)
        sids.append(sid)
    started = pids(env.pid_log)
    decoders = [p for p in started if alive(p)]
    assert len(decoders) == 3 and env.hub.subscriber_count(1) == 3
    await wm.close_session("s0")
    assert sum(alive(p) for p in decoders) == 2
    t0 = time.monotonic()
    await wm.stop_all_sessions(timeout=5.0)
    assert time.monotonic() - t0 < 5.0
    assert not any(alive(p) for p in decoders)
    assert env.hub.subscriber_count(1) == 0 and wm.session._sessions == {} and not procs._live
    assert ownership.Registry.load_previous(procs.registry_path())[1] == []
    assert ended == []


async def test_orphans_of_a_crashed_run_are_reaped_at_startup(env):
    env.feed_init()
    track, _ = make_track()
    await asyncio.wait_for(track.recv(), 5)
    pid = track.owned.pid
    # the app "crashes": registry stays on disk, nobody kills the decoder
    track._tasks and [t.cancel() for t in track._tasks]
    procs._registry = None
    procs._live.clear()
    assert alive(pid)
    await procs.startup()
    assert await wait_until(lambda: not alive(pid))
    assert ownership.Registry.load_previous(procs.registry_path())[1] == []  # fresh registry


async def test_foreign_registry_entry_is_not_killed(env):
    sleeper = await asyncio.create_subprocess_exec("sleep", "30")
    try:
        reg = ownership.Registry(procs.registry_path())
        reg.add(7, sleeper.pid, ownership.read_start_time(sleeper.pid))  # no tag in its env
        await procs.startup()
        assert sleeper.returncode is None and alive(sleeper.pid)
    finally:
        sleeper.kill()
        await sleeper.wait()


async def test_receive_or_end_helper():
    from app.routers.cams.webrtc import _receive_or_end

    class WS:
        def __init__(self):
            self.q = asyncio.Queue()

        async def receive_json(self):
            return await self.q.get()

    ws, ended = WS(), asyncio.Event()
    ws.q.put_nowait({"type": "answer"})
    assert await _receive_or_end(ws, ended) == {"type": "answer"}
    asyncio.get_running_loop().call_later(0.05, ended.set)
    assert await asyncio.wait_for(_receive_or_end(ws, ended), 2) is None
    ws.q.put_nowait({"type": "late"})  # nothing was swallowed by the cancelled receive
    assert await _receive_or_end(ws, asyncio.Event()) == {"type": "late"}
