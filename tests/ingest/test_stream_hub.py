"""stream_manager with the hub source (stage 2), on the fake ffmpeg.

Ingest is the real supervisor + hub running `fake_ffmpeg.py` (mode `ok`); the transcoder
is the same fake in its stdin mode (`-i pipe:0`)."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import pytest
import pytest_asyncio

from app.core.config import settings
from app.models.cams import Cam
from app.services import stream_manager as sm
from app.services.ingest import lifecycle, ownership
from tests.ingest.conftest import FAKE, alive, starts, target, wait_until

pytestmark = pytest.mark.asyncio


def cam(cam_id=1, path="/ok?rate=20&gop=5"):
    return Cam(id=cam_id, addr="127.0.0.1", port=554, path=path, username="user", password="secret")


@pytest.fixture
def args_log(tmp_path, monkeypatch):
    log = tmp_path / "args.log"
    monkeypatch.setenv("FAKE_FFMPEG_ARGS_LOG", str(log))
    return log


def transcoder_runs(log):
    return [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []


@pytest_asyncio.fixture
async def mgr(sup, fake_log, args_log, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_ENABLED", True)
    monkeypatch.setattr(settings, "INGEST_REGISTRY_DIR", str(tmp_path / "ingest"))
    monkeypatch.setattr(settings, "INGEST_IDLE_TRANSCODE_LINGER_SECONDS", 0.8)
    monkeypatch.setattr(settings, "INGEST_HUB_WAIT_SECONDS", 0.6)
    monkeypatch.setattr(settings, "STREAM_IDLE_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(sm, "_TERM_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(sm, "_KILL_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(lifecycle, "_supervisor", sup)
    sm._sessions.clear()
    sm._cam_locks.clear()
    sm._registry = None
    sm._guard = asyncio.Lock()
    await sup.start(target(1, "ok", rate=20, gop=5))
    assert await wait_until(lambda: sup.hub.get(1).online.is_set())
    yield sm
    await sm.stop_all()
    sm._sessions.clear()
    sm._registry = None
    for pid, _ in starts(fake_log):
        if alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass


async def first_chunks(queue, n=3, timeout=5.0):
    out = []
    async def go():
        async for c in sm.iter_queue(queue):
            out.append(c)
            if len(out) >= n:
                return
    await asyncio.wait_for(go(), timeout)
    return out


async def test_first_viewer_starts_transcoder_from_hub(mgr, args_log):
    queue, session = await sm.get_or_attach(cam())
    assert session.source == "hub" and session.pid
    chunks = await first_chunks(queue)
    assert chunks[0][4:8] == b"ftyp"
    args = transcoder_runs(args_log)[0]
    assert args[args.index("-i") + 1] == "pipe:0"
    assert "-re" not in args and not any(a.startswith("rtsp") for a in args)
    assert args[args.index("-c:v") + 1] == "libx264"
    assert args[args.index("-c:a") + 1] == "copy"
    assert args[args.index("-movflags") + 1] == "frag_keyframe+empty_moov+default_base_moof"
    assert "-nostdin" in args and "nobuffer" not in args
    # registered with start time, so it can be reaped after a crash of the app
    assert ownership.Registry.load_previous(sm._registry_path())[1][0].pid == session.pid
    env = ownership.read_environ(session.pid)
    assert env[ownership.ENV_TAG].endswith(":1")
    await sm.detach(1, session, queue)


async def test_one_transcoder_shared_by_viewers(mgr, args_log):
    q1, s1 = await sm.get_or_attach(cam())
    q2, s2 = await sm.get_or_attach(cam())
    assert s1 is s2 and await sm.has_active_session(1)
    assert len(transcoder_runs(args_log)) == 1
    for q in (q1, q2):
        assert (await first_chunks(q, 2))[0][4:8] == b"ftyp"  # each gets the init first
    await sm.detach(1, s1, q1)
    await sm.detach(1, s1, q2)


async def test_linger_then_stop_ingest_keeps_running(mgr, args_log, sup):
    q, s = await sm.get_or_attach(cam())
    await first_chunks(q, 2)
    await sm.detach(1, s, q)
    await asyncio.sleep(0.3)
    assert alive(s.pid) and sm._sessions.get(1) is s  # lingering
    assert sup.hub.subscriber_count(1) == 1  # transcoder still subscribed
    assert await wait_until(lambda: not alive(s.pid), timeout=4.0)
    assert await wait_until(lambda: 1 not in sm._sessions)
    assert await wait_until(lambda: sup.hub.subscriber_count(1) == 0)
    assert sup.is_running(1) and sup.hub.get(1).online.is_set()  # ingest untouched
    assert ownership.Registry.load_previous(sm._registry_path())[1] == []


async def test_viewer_in_linger_attaches_instantly(mgr, args_log):
    q, s = await sm.get_or_attach(cam())
    await first_chunks(q, 2)
    await sm.detach(1, s, q)
    await asyncio.sleep(0.3)
    t0 = time.monotonic()
    q2, s2 = await sm.get_or_attach(cam())
    assert time.monotonic() - t0 < 0.1
    assert s2 is s and len(transcoder_runs(args_log)) == 1
    assert (await first_chunks(q2, 2))[0][4:8] == b"ftyp"
    await asyncio.sleep(1.2)  # idle timer was cancelled: still alive past the linger
    assert alive(s.pid)
    await sm.detach(1, s2, q2)


async def test_fallback_when_ingest_disabled(mgr, monkeypatch, args_log):
    monkeypatch.setattr(settings, "INGEST_ENABLED", False)
    q, s = await sm.get_or_attach(cam())
    assert s.source == "direct" and transcoder_runs(args_log) == []
    assert (await first_chunks(q, 2))[0][4:8] == b"ftyp"
    await sm.detach(1, s, q)


async def test_fallback_when_cam_not_in_ingest(mgr, args_log):
    q, s = await sm.get_or_attach(cam(cam_id=7))
    assert s.source == "direct" and transcoder_runs(args_log) == []
    await sm.detach(7, s, q)


async def test_fallback_when_hub_not_online_in_time(mgr, sup, args_log):
    await sup.start(target(3, "silent"))
    t0 = time.monotonic()
    q, s = await sm.get_or_attach(cam(cam_id=3))
    assert s.source == "direct" and transcoder_runs(args_log) == []
    assert 0.5 <= time.monotonic() - t0 < 3.0  # waited INGEST_HUB_WAIT_SECONDS
    await sm.detach(3, s, q)


@pytest.mark.parametrize("mode", ["reject", "silent"])
async def test_fallback_when_transcoder_fails(mgr, monkeypatch, mode):
    monkeypatch.setenv("FAKE_TRANSCODER_MODE", mode)
    monkeypatch.setattr(sm, "_FIRST_CHUNK_TIMEOUT_SECONDS", 1.0)
    q, s = await sm.get_or_attach(cam())
    assert s.source == "direct"
    await sm.detach(1, s, q)


def sup_subscribers(cam_id):
    return lifecycle.get_supervisor().hub.subscriber_count(cam_id)


async def test_failed_transcoder_releases_hub_and_process(mgr, monkeypatch, fake_log):
    monkeypatch.setenv("FAKE_TRANSCODER_MODE", "silent")
    monkeypatch.setattr(sm, "_FIRST_CHUNK_TIMEOUT_SECONDS", 0.8)
    before = {p for p, _ in starts(fake_log)}
    q, s = await sm.get_or_attach(cam())
    assert s.source == "direct"
    assert sup_subscribers(1) == 0
    await sm.stop_all()
    pids = [p for p, _ in starts(fake_log) if p not in before]
    assert await wait_until(lambda: not any(alive(p) for p in pids))


async def test_ingest_restart_ends_viewers_and_next_viewer_gets_new_transcoder(mgr, sup, args_log):
    q, s = await sm.get_or_attach(cam())
    await first_chunks(q, 2)
    assert await sup.restart(1)
    rest = []
    async def drain():
        async for c in sm.iter_queue(q):
            rest.append(c)
    await asyncio.wait_for(drain(), 5.0)  # iterator ended -> the HTTP response ends
    assert await wait_until(lambda: not alive(s.pid))
    assert await wait_until(lambda: 1 not in sm._sessions)
    await sm.detach(1, s, q)  # what the endpoint's finally does: must be harmless
    assert await wait_until(lambda: sup.hub.get(1).online.is_set())
    q2, s2 = await sm.get_or_attach(cam())
    assert s2 is not s and s2.source == "hub" and s2.pid != s.pid
    assert (await first_chunks(q2, 2))[0][4:8] == b"ftyp"
    assert len(transcoder_runs(args_log)) == 2
    await sm.detach(1, s2, q2)


async def test_overflow_of_feed_ends_session(mgr, sup):
    q, s = await sm.get_or_attach(cam())
    await first_chunks(q, 1)
    s.sub._finish("overflow")  # hub cut the transcoder's subscription off
    await asyncio.wait_for(_drain(q), 5.0)
    assert await wait_until(lambda: not alive(s.pid))
    assert s.end_reason == "overflow"


async def _drain(q):
    async for _ in sm.iter_queue(q):
        pass


async def test_transcoder_crash_closes_viewers_and_masks_password(mgr):
    os.environ["FAKE_TRANSCODER_MODE"] = "crash"
    try:
        q, s = await sm.get_or_attach(cam())
        await asyncio.wait_for(_drain(q), 5.0)
        assert await wait_until(lambda: 1 not in sm._sessions)
        assert "secret" not in s.last_stderr_line and "boom" in s.last_stderr_line
    finally:
        os.environ.pop("FAKE_TRANSCODER_MODE", None)


async def test_stop_all_bounded_with_sigterm_ignoring_transcoder(mgr, monkeypatch):
    monkeypatch.setenv("FAKE_TRANSCODER_MODE", "hang")
    q, s = await sm.get_or_attach(cam())
    await first_chunks(q, 1)
    t0 = time.monotonic()
    await sm.stop_all()
    assert time.monotonic() - t0 < 4.0
    assert await wait_until(lambda: not alive(s.pid), timeout=2.0)
    await asyncio.wait_for(_drain(q), 2.0)  # viewers released


async def test_stop_by_cam_id(mgr):
    q, s = await sm.get_or_attach(cam())
    await sm.stop(1)
    assert not alive(s.pid) and not await sm.has_active_session(1)
    await asyncio.wait_for(_drain(q), 2.0)


async def test_no_process_leak(mgr, sup, fake_log):
    for _ in range(3):
        q, s = await sm.get_or_attach(cam())
        await first_chunks(q, 1)
        await sm.detach(1, s, q)
        await sm.stop(1)
    await sm.stop_all()
    await sup.stop_all(timeout=4.0)
    pids = [p for p, _ in starts(fake_log)]
    assert len(pids) >= 4
    assert await wait_until(lambda: not any(alive(p) for p in pids))


async def test_startup_reaps_only_verified_orphans(mgr, monkeypatch, tmp_path):
    """A transcoder left by the previous run is killed; a stranger whose pid is in the
    registry (no env tag) is not."""
    inst = "deadbeefdeadbeef"
    env = {**os.environ, ownership.ENV_TAG: ownership.make_tag(inst, 5)}
    cmd = [FAKE, "-i", "pipe:0"]
    orphan = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, env=env, start_new_session=True)
    stranger = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, start_new_session=True)
    try:
        monkeypatch.setattr(settings, "FFMPEG_BINARY", FAKE)
        await asyncio.sleep(0.3)
        path = sm._registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"instance_id": inst, "entries": [
            {"cam_id": 5, "pid": orphan.pid, "start_time": ownership.read_start_time(orphan.pid)},
            {"cam_id": 6, "pid": stranger.pid, "start_time": ownership.read_start_time(stranger.pid)},
        ]}))
        sm._registry = None
        await sm.startup()
        assert await wait_until(lambda: orphan.poll() is not None, timeout=5.0)
        assert stranger.poll() is None and alive(stranger.pid)
        assert ownership.Registry.load_previous(path)[1] == []
    finally:
        for p in (orphan, stranger):
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()
