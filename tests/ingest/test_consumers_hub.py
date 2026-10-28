"""Stage 5: live snapshots and motion detection on the hub, on the fake ffmpeg/ffprobe
(tests/fake_ffmpeg_consumer.py). Real ffmpeg: test_consumers_real.py."""
import asyncio
import json
import logging
import os
import signal
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.core.config import settings
from app.models.cams import Cam
from app.services import snapshot
from app.services.ingest import lifecycle, ownership
from app.services.ingest.hub import IngestHub
from app.services.ingest.supervisor import IngestSupervisor
from app.services.motion_detection import manager as md_manager
from app.services.motion_detection import session as md_session
from app.services.motion_detection.session import MotionDetectionSession
from tests.fmp4_builder import fragment, init_segment
from tests.ingest.conftest import alive, wait_until

FAKE = str(Path(__file__).resolve().parent.parent / "fake_ffmpeg_consumer.py")
W, H = 64, 48
FRAME = W * H * 3


def pids(log: Path) -> list[int]:
    return [int(ln.split()[0]) for ln in log.read_text().splitlines()] if log.exists() else []


def runs(log: Path) -> list[list[str]]:
    return [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []


def ffmpeg_runs(log: Path) -> list[list[str]]:
    return [a for a in runs(log) if "-show_entries" not in a]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FFMPEG_ARGS_LOG", str(tmp_path / "args.log"))
    monkeypatch.setenv("FAKE_FFMPEG_LOG", str(tmp_path / "pids.log"))
    monkeypatch.setenv("FAKE_HC_SIZE", f"{W}x{H}")
    monkeypatch.setenv("FAKE_HC_INIT_BYTES", str(len(init_segment())))
    monkeypatch.setenv("FAKE_HC_COUNTER", str(tmp_path / "counter"))
    monkeypatch.setattr(settings, "FFMPEG_BINARY", FAKE)
    monkeypatch.setattr(settings, "FFPROBE_BINARY", FAKE)
    monkeypatch.setattr(settings, "INGEST_ENABLED", True)
    monkeypatch.setattr(settings, "INGEST_REGISTRY_DIR", str(tmp_path / "ingest"))
    monkeypatch.setattr(settings, "INGEST_HUB_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(settings, "INGEST_SNAPSHOT_HUB_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(settings, "INGEST_SNAPSHOT_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(settings, "INGEST_MOTION_FIRST_FRAME_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(settings, "INGEST_MOTION_STALL_SECONDS", 2.0)
    monkeypatch.setattr(settings, "MOTION_FILES_DIR", str(tmp_path / "motion"))
    monkeypatch.setattr(md_session, "_RETRY_INITIAL_SECONDS", 0.05)
    for pool in (snapshot._pool, md_session.pool):
        pool.term_timeout, pool.kill_timeout = 0.4, 1.0
        pool.registry = None
        pool.live.clear()
    snapshot._hub_last.clear()
    snapshot._hub_locks.clear()
    hub = IngestHub()
    sup = IngestSupervisor(hub=hub)
    running = {1: True}
    sup.is_running = lambda cam_id: running.get(cam_id, False)
    monkeypatch.setattr(lifecycle, "_supervisor", sup)
    monkeypatch.setattr(md_session, "_get_yolo_model", lambda: object())
    e = SimpleNamespace(hub=hub, running=running, args_log=tmp_path / "args.log", pid_log=tmp_path / "pids.log",
                        tmp=tmp_path, mp=monkeypatch)
    e.warm = lambda cam_id=1: hub.get(cam_id).feed(init_segment() + fragment(0) + fragment(1, key=False))
    yield e
    snapshot._pool.force_kill_all()
    md_session.pool.force_kill_all()
    snapshot._pool.registry = md_session.pool.registry = None
    for pid in pids(e.pid_log):
        if alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass


def make_cam(**kw):
    base = dict(id=1, name="c", proto="rtsp", addr="127.0.0.1", port=554, path="/x", username="user",
                password="secret", onvif_enabled=0, onvif_snapshot_enabled=0)
    base.update(kw)
    return Cam(**base)


# ------------------------------------------------------------------ snapshots

async def test_snapshot_from_hub_uses_init_and_last_gop_without_the_camera(env, caplog):
    caplog.set_level(logging.INFO)
    dump = env.tmp / "stdin.bin"
    env.mp.setenv("FAKE_HC_JPEG_STDIN", str(dump))
    env.warm()
    data = await snapshot._grab_from_hub(make_cam())
    assert data == b"\xff\xd8\xff\x00\x02\xff\xd9"  # the NEWEST of the decoded frames
    argv = ffmpeg_runs(env.args_log)
    assert len(argv) == 1 and not any(a.startswith("rtsp") for a in argv[0])
    a = argv[0]
    assert a[a.index("-i") + 1] == "pipe:0" and "-re" not in a and "nobuffer" not in " ".join(a)
    assert "mjpeg" in a and "scale='min(1280,iw)':-2" in a and "-an" in a
    assert dump.read_bytes() == init_segment() + fragment(0) + fragment(1, key=False)
    assert "snapshot: cam 1 source=hub first_byte_ms=" in caplog.text
    assert "secret" not in caplog.text
    # no subscriber was added (a snapshot must not hold back planned restarts)
    assert env.hub.subscriber_count(1) == 0
    assert await wait_until(lambda: not any(alive(p) for p in pids(env.pid_log)))
    assert not snapshot._pool.live
    assert ownership.Registry.load_previous(snapshot._pool.registry_path())[1] == []
    assert snapshot._pool.registry_path().name == "snapshot_registry.json"


async def test_snapshot_same_gop_is_decoded_once_and_a_new_gop_again(env):
    env.warm()
    cam = make_cam()
    results = await asyncio.gather(*[snapshot._grab_from_hub(cam) for _ in range(6)])
    assert len(set(results)) == 1 and results[0]
    assert len(ffmpeg_runs(env.args_log)) == 1
    env.hub.get(1).feed(fragment(2, key=True, size=2000))  # next GOP
    assert await snapshot._grab_from_hub(cam)
    assert len(ffmpeg_runs(env.args_log)) == 2


async def test_live_snapshot_prefers_hub_over_rtsp_and_keeps_onvif_first(env, monkeypatch):
    env.warm()
    rtsp_calls = []

    async def rtsp(cam):
        rtsp_calls.append(cam.id)
        return b"RTSP"

    monkeypatch.setattr(snapshot, "_grab_from_rtsp", rtsp)
    assert (await snapshot.get_live_snapshot(make_cam())).startswith(b"\xff\xd8")
    assert rtsp_calls == []
    # ONVIF enabled and working: still first (user's explicit choice), hub untouched
    from app.services import onvif_client

    async def onvif(cam):
        return b"ONVIF"

    monkeypatch.setattr(onvif_client, "get_snapshot", onvif)
    before = len(runs(env.args_log))
    assert await snapshot.get_live_snapshot(make_cam(onvif_enabled=1, onvif_snapshot_enabled=1)) == b"ONVIF"
    assert len(runs(env.args_log)) == before


@pytest.mark.parametrize("why", ["disabled", "not_running", "hub_offline", "decode_empty", "decode_crash", "timeout"])
async def test_snapshot_falls_back_to_rtsp(env, monkeypatch, caplog, why):
    caplog.set_level(logging.INFO)
    env.warm()
    if why == "disabled":
        monkeypatch.setattr(settings, "INGEST_ENABLED", False)
    elif why == "not_running":
        env.running[1] = False
    elif why == "hub_offline":
        env.hub.get(1).reset_stream("test")
    elif why == "decode_empty":
        monkeypatch.setenv("FAKE_HC_MODE", "silent")
    elif why == "decode_crash":
        monkeypatch.setenv("FAKE_HC_MODE", "crash")
    elif why == "timeout":
        monkeypatch.setenv("FAKE_HC_MODE", "hang")  # ignores SIGTERM: escalates to SIGKILL
        monkeypatch.setattr(settings, "INGEST_SNAPSHOT_TIMEOUT_SECONDS", 0.5)

    async def rtsp(cam):
        return b"RTSP"

    monkeypatch.setattr(snapshot, "_grab_from_rtsp", rtsp)
    assert await snapshot.get_live_snapshot(make_cam()) == b"RTSP"
    assert "source=rtsp first_byte_ms=" in caplog.text
    assert "secret" not in caplog.text
    if why in ("decode_empty", "decode_crash", "timeout"):
        # a timed-out decoder is ended in the background (SIGTERM, then SIGKILL)
        assert await wait_until(lambda: not snapshot._pool.live and not any(alive(p) for p in pids(env.pid_log)), timeout=5)
        assert ownership.Registry.load_previous(snapshot._pool.registry_path())[1] == []
    if why == "disabled":
        assert runs(env.args_log) == []


async def test_snapshot_waits_briefly_for_a_gop_then_gives_up(env):
    env.hub.get(1).feed(init_segment())  # online, but not a single fragment yet
    t0 = time.monotonic()
    assert await snapshot._grab_from_hub(make_cam()) is None
    assert 0.25 <= time.monotonic() - t0 < 2.0

    async def late():
        await asyncio.sleep(0.1)
        env.hub.get(1).feed(fragment(0))

    task = asyncio.create_task(late())
    assert await snapshot._grab_from_hub(make_cam())
    await task


async def test_snapshot_shutdown_kills_running_decoders_and_orphans_are_reaped(env, monkeypatch):
    env.warm()
    monkeypatch.setenv("FAKE_HC_MODE", "hang")
    cam = make_cam()
    task = asyncio.create_task(snapshot._grab_from_hub(cam))
    assert await wait_until(lambda: len(pids(env.pid_log)) == 1 and snapshot._pool.live)
    pid = pids(env.pid_log)[0]
    assert alive(pid)
    await snapshot.stop_all(timeout=3.0)
    assert await wait_until(lambda: not alive(pid), timeout=3)
    assert await asyncio.wait_for(task, 5) is None  # killed decoder: no frame -> caller falls back
    # a crashed app: registry on disk, process still running -> reaped by startup()
    monkeypatch.setenv("FAKE_HC_MODE", "hang")
    task = asyncio.create_task(snapshot._grab_from_hub(make_cam()))
    assert await wait_until(lambda: len(pids(env.pid_log)) == 2 and snapshot._pool.live)
    pid2 = pids(env.pid_log)[1]
    task.cancel()  # the request is gone, but pretend the app died before terminating
    snapshot._pool.registry = None
    snapshot._pool.live.clear()
    await asyncio.sleep(0)
    await snapshot.startup()
    assert await wait_until(lambda: not alive(pid2), timeout=5)


# ------------------------------------------------------------------ motion detection

def make_session(**kw):
    ps = SimpleNamespace(user_id=1, motion_detect_person=1, motion_detect_dog=1, motion_detect_cat=1,
                         motion_detect_car=1, motion_min_confidence=0.5, motion_min_object_size=0.01,
                         motion_cooldown_seconds=0, motion_license_plate_ocr=0)
    session = MotionDetectionSession(make_cam(**kw), [ps])
    seen = []

    async def process(frame, model, n, ts=None):
        seen.append(SimpleNamespace(n=n, ts=ts, marker=struct.unpack(">Q", bytes(frame[0, :8 // 3 + 1].tobytes()[:8]))[0],
                                    shape=frame.shape, at=time.time()))

    session._process_frame = process
    session.seen = seen
    return session


def decoder_run(env):
    return [a for a in ffmpeg_runs(env.args_log) if "pipe:0" in a]


async def test_motion_from_hub_skips_stored_gop_and_stamps_bursts(env, caplog):
    caplog.set_level(logging.INFO)
    env.warm()  # stale GOP: fragment(0) + fragment(1)
    s = make_session()
    await s.start()
    assert await wait_until(lambda: env.hub.subscriber_count(1) == 1 and s.source == "hub" and pids(env.pid_log))
    live = fragment(2, key=True, size=2000)
    t0 = time.time()
    env.hub.get(1).feed(live)
    assert await wait_until(lambda: len(s.seen) == 3)
    # the decoder saw the init and the live fragment only: the stale GOP was not replayed
    assert {f.marker for f in s.seen} == {len(init_segment()) + len(live)}
    assert all(f.shape == (H, W, 3) for f in s.seen)
    # estimated capture times: 1 s apart, the newest one is "now"
    ts = [f.ts for f in s.seen]
    assert ts[1] - ts[0] == pytest.approx(1.0, abs=0.01) and ts[2] - ts[1] == pytest.approx(1.0, abs=0.01)
    assert t0 - 0.5 <= ts[2] <= time.time() + 0.1
    a = decoder_run(env)[0]
    assert a[a.index("-i") + 1] == "pipe:0" and "fps=1" in a and "bgr24" in a and "rawvideo" in a
    assert "-re" not in a and "nobuffer" not in " ".join(a)
    assert not any(x.startswith("rtsp") for run in ffmpeg_runs(env.args_log) + runs(env.args_log) for x in run)
    assert "source=hub first_byte_ms=" in caplog.text and "secret" not in caplog.text
    entry = ownership.Registry.load_previous(md_session.pool.registry_path())[1][0]
    assert md_session.pool.registry_path().name == "motion_registry.json"
    assert ownership.read_environ(entry.pid)[ownership.ENV_TAG].endswith(f":{entry.cam_id}")
    pid = entry.pid
    await s.stop()
    assert not alive(pid) and env.hub.subscriber_count(1) == 0
    assert ownership.Registry.load_previous(md_session.pool.registry_path())[1] == []
    assert not md_session.pool.live


async def test_motion_drops_frames_that_are_too_old(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(settings, "INGEST_MOTION_MAX_FRAME_AGE_SECONDS", 1.5)
    env.warm()
    s = make_session()
    await s.start()
    assert await wait_until(lambda: env.hub.subscriber_count(1) == 1 and pids(env.pid_log))
    env.hub.get(1).feed(fragment(2, key=True, size=2000))
    # burst of 3 stamped now-2, now-1, now: the first one is older than 1.5 s
    assert await wait_until(lambda: len(s.seen) == 2)
    assert [f.n for f in s.seen] == [2, 3]
    assert "is " in caplog.text and "old, skipped" in caplog.text
    await s.stop()


async def test_motion_resumes_after_ingest_restart_on_the_new_stream(env, monkeypatch):
    env.warm()
    s = make_session()
    await s.start()
    assert await wait_until(lambda: env.hub.subscriber_count(1) == 1 and pids(env.pid_log))
    first = pids(env.pid_log)[0]
    env.hub.get(1).feed(fragment(2, key=True, size=2000))
    assert await wait_until(lambda: len(s.seen) == 3)
    env.hub.get(1).reset_stream("restart")  # ingest ffmpeg restarted: new timeline
    assert await wait_until(lambda: not alive(first))
    env.hub.get(1).feed(init_segment() + fragment(0))  # new stream online
    assert await wait_until(lambda: env.hub.subscriber_count(1) == 1 and len(pids(env.pid_log)) == 2)
    env.hub.get(1).feed(fragment(1, key=True, size=3000))
    assert await wait_until(lambda: len(s.seen) == 6)
    assert s.is_running
    await s.stop()
    assert not any(alive(p) for p in pids(env.pid_log))


async def test_motion_falls_back_to_direct_rtsp_when_there_is_no_hub(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    for why in ("disabled", "not_running"):
        env.args_log.write_text("")
        if why == "disabled":
            monkeypatch.setattr(settings, "INGEST_ENABLED", False)
        else:
            monkeypatch.setattr(settings, "INGEST_ENABLED", True)
            env.running[1] = False
        env.warm()
        s = make_session()
        await s.start()
        assert await wait_until(lambda: len(s.seen) == 3)
        assert s.source == "rtsp" and s.seen[0].shape == (H, W, 3)
        a = [x for x in ffmpeg_runs(env.args_log)][0]
        assert "-rtsp_transport" in a and "fps=1" in a and "bgr24" in a and "-s" in a
        assert "source=rtsp first_byte_ms=" in caplog.text and "secret" not in caplog.text
        pid = pids(env.pid_log)[-1]
        await s.stop()
        assert await wait_until(lambda: not alive(pid))
        assert env.hub.subscriber_count(1) == 0


async def test_motion_hub_failure_before_first_frame_falls_back_to_direct(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("FAKE_HC_MODE", "hubcrash")
    env.warm()
    s = make_session()
    await s.start()
    assert await wait_until(lambda: len(s.seen) == 3)
    assert s.source == "rtsp"
    assert "hub source failed" in caplog.text and "secret" not in caplog.text
    assert env.hub.subscriber_count(1) == 0
    await s.stop()


async def test_motion_silent_hub_decoder_falls_back_after_first_frame_timeout(env, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_MOTION_FIRST_FRAME_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setenv("FAKE_HC_BURST", "0")  # decoder reads but never writes a frame
    env.warm()
    s = make_session()
    await s.start()
    assert await wait_until(lambda: env.hub.subscriber_count(1) == 1 and pids(env.pid_log))
    hub_pid = pids(env.pid_log)[0]
    assert await wait_until(lambda: not alive(hub_pid), timeout=5)  # given up, decoder killed
    monkeypatch.setenv("FAKE_HC_BURST", "3")
    assert await wait_until(lambda: len(s.seen) == 3 and s.source == "rtsp", timeout=5)
    await s.stop()


async def test_motion_truncated_first_frame_is_retried(env, monkeypatch, caplog):
    """The 'camera 9' case: the source dies mid-frame. The session used to end for good."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(settings, "INGEST_ENABLED", False)
    monkeypatch.setenv("FAKE_HC_MODE", "trunc")
    s = make_session()
    await s.start()
    assert await wait_until(lambda: len(s.seen) == 3, timeout=5)
    assert len(ffmpeg_runs(env.args_log)) == 2 and s.is_running
    assert "Incomplete frame" in caplog.text
    await s.stop()


async def test_motion_gives_up_after_repeated_failures_and_masks_passwords(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(settings, "INGEST_ENABLED", False)
    monkeypatch.setattr(settings, "INGEST_MOTION_RESTART_ATTEMPTS", 3)
    monkeypatch.setenv("FAKE_HC_MODE", "crash")
    s = make_session()
    await s.start()
    assert await wait_until(lambda: not s.is_running, timeout=5)
    assert len(ffmpeg_runs(env.args_log)) == 3
    assert "ffmpeg ended" in caplog.text and "boom" in caplog.text
    assert "secret" not in caplog.text
    await s.stop()


async def test_motion_stop_all_leaves_no_processes(env, monkeypatch):
    monkeypatch.setenv("FAKE_HC_MODE", "hang")
    env.warm()
    s = make_session()
    md_manager._active_sessions[1] = s
    try:
        await s.start()
        assert await wait_until(lambda: env.hub.subscriber_count(1) == 1 and pids(env.pid_log))
        env.hub.get(1).feed(fragment(2, key=True, size=2000))
        assert await wait_until(lambda: len(s.seen) == 3)
        started = pids(env.pid_log)
        assert any(alive(p) for p in started)
        t0 = time.monotonic()
        await md_manager.stop_all_sessions()
        assert time.monotonic() - t0 < 6.0
        assert not any(alive(p) for p in started)
        assert md_manager._active_sessions == {} and not md_session.pool.live
        assert env.hub.subscriber_count(1) == 0
        assert ownership.Registry.load_previous(md_session.pool.registry_path())[1] == []
    finally:
        md_manager._active_sessions.pop(1, None)


async def test_motion_orphans_of_a_crashed_run_are_reaped(env, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_ENABLED", True)
    env.warm()
    s = make_session()
    await s.start()
    assert await wait_until(lambda: env.hub.subscriber_count(1) == 1 and pids(env.pid_log))
    pid = pids(env.pid_log)[0]
    assert alive(pid)
    # the app "crashes": registry stays on disk, nobody kills the decoder
    s.task.cancel()
    s._owned.clear()
    md_session.pool.registry = None
    md_session.pool.live.clear()
    await md_manager.startup()
    assert await wait_until(lambda: not alive(pid), timeout=5)
    assert ownership.Registry.load_previous(md_session.pool.registry_path())[1] == []


async def test_foreign_registry_entry_is_not_killed(env):
    sleeper = await asyncio.create_subprocess_exec("sleep", "30")
    try:
        for pool in (snapshot._pool, md_session.pool):
            reg = ownership.Registry(pool.registry_path())
            reg.add(7, sleeper.pid, ownership.read_start_time(sleeper.pid))  # no tag in its env
            await pool.startup()
            assert sleeper.returncode is None and alive(sleeper.pid)
    finally:
        sleeper.kill()
        await sleeper.wait()


def test_tracker_accepts_frame_times():
    from app.services.object_tracker import ObjectTracker

    t = ObjectTracker()
    t.update([("person", (0, 0, 10, 10), 0.9)], now=100.0)
    t.update([("person", (2, 0, 10, 10), 0.9)], now=101.0)
    assert t.get_moving_objects(0.5, now=101.0) == []  # moved 2 px: not "moving"
    t.update([("person", (4, 0, 10, 10), 0.9)], now=500.0)  # old track expired: a new one
    assert len(t.tracked_objects) == 1 and next(iter(t.tracked_objects.values())).first_seen == 500.0
