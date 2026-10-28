import asyncio
import logging
import os
import signal
import time

from app.services.fmp4 import Fmp4Splitter
from app.services.ingest import ownership as ow
from app.services.ingest.hub import STATE_ONLINE
from tests.fmp4_builder import init_segment
from tests.ingest.conftest import alive, starts, target, wait_until


async def online(sup, cam_id=1, timeout=8.0):
    hub = sup.hub.get(cam_id)
    assert await wait_until(lambda: hub.state == STATE_ONLINE, timeout), hub.status()
    return hub


async def test_start_feeds_hub_and_stop_kills_process(sup, fake_log):
    await sup.start(target(1))
    hub = await online(sup)
    pid = sup.get_status()[1]["pid"]
    assert pid and alive(pid)
    # registry knows it
    _, entries = ow.Registry.load_previous(sup.config.registry_path)
    assert [(e.cam_id, e.pid) for e in entries] == [(1, pid)]
    # a viewer gets init + media right away
    async with sup.hub.subscribe(1) as sub:
        first = await asyncio.wait_for(sub.__anext__(), 2)
        assert first == init_segment()
        assert (await asyncio.wait_for(sub.__anext__(), 2)).startswith(b"\x00") 
    await sup.stop(1)
    assert not alive(pid)
    assert not sup.is_running(1) and hub.state == "offline"
    _, entries = ow.Registry.load_previous(sup.config.registry_path)
    assert entries == []


async def test_apply_none_stops_and_same_url_is_noop_changed_url_restarts(sup, fake_log):
    assert await sup.apply(1, target(1)) == "started"
    await online(sup)
    pid1 = sup.get_status()[1]["pid"]
    assert await sup.apply(1, target(1)) == "unchanged"
    assert sup.get_status()[1]["pid"] == pid1
    assert await sup.apply(1, target(1, rate=10)) == "restarting"
    assert await wait_until(lambda: len(starts(fake_log)) == 2)
    await online(sup)
    assert not alive(pid1)
    assert await sup.apply(1, None) == "stopped"
    assert not sup.is_running(1)
    assert not alive(starts(fake_log)[1][0])


async def test_crash_restarts_with_growing_backoff_capped(sup, fake_log):
    await sup.start(target(1, "crash", frags=1))
    assert await wait_until(lambda: len(starts(fake_log)) >= 5, 10)
    times = [t for _, t in starts(fake_log)]
    gaps = [b - a for a, b in zip(times, times[1:])]
    # initial 0.2, then 0.4, 0.8 (cap) - gaps include process start overhead
    assert 0.18 <= gaps[0] < 0.5
    assert gaps[1] > gaps[0] + 0.1
    assert gaps[2] > gaps[1] + 0.1
    assert gaps[3] < 1.3  # capped at 0.8 (+ overhead), not 1.6
    assert sup.hub.get(1).restarts >= 4
    assert "exited" in sup.hub.get(1).last_error


async def test_backoff_resets_after_stable_run(sup, fake_log):
    # two fast failures (backoff grows to 0.4), then a stable run
    await sup.start(target(1, "flaky", fail=2))
    hub = await online(sup)
    await asyncio.sleep(0.8)  # > stable_seconds with data flowing
    pid = sup.get_status()[1]["pid"]
    killed_at = time.time()
    os.killpg(pid, signal.SIGKILL)
    assert await wait_until(lambda: len(starts(fake_log)) == 4)
    assert starts(fake_log)[3][1] - killed_at < 0.6  # initial backoff again, not 0.8
    await online(sup)


async def test_flaky_recovers_after_failures(sup, fake_log):
    await sup.start(target(1, "flaky", fail=2))
    await online(sup)
    assert len(starts(fake_log)) == 3


async def test_watchdog_kills_silent_process(sup, fake_log):
    await sup.start(target(1, "silent"))
    assert await wait_until(lambda: len(starts(fake_log)) >= 1)
    pid1 = starts(fake_log)[0][0]
    assert await wait_until(lambda: len(starts(fake_log)) >= 2, 6)
    assert not alive(pid1)
    assert "no data" in sup.hub.get(1).last_error


async def test_watchdog_kills_stalled_stream_and_ends_subscribers(sup, fake_log):
    await sup.start(target(1, "stall", frags=2))
    hub = await online(sup)
    sub = sup.hub.subscribe(1)
    pid1 = starts(fake_log)[0][0]
    t0 = time.monotonic()
    chunks = [c async for c in sub]  # ends when the watchdog restarts the stream
    assert 0.8 < time.monotonic() - t0 < 4
    assert len(chunks) >= 2  # init + the stored GOP (every fragment is a keyframe)
    assert sub.close_reason == "restart"
    assert await wait_until(lambda: len(starts(fake_log)) >= 2, 4)
    assert not alive(pid1)


async def test_planned_restart_only_without_viewers(sup, fake_log):
    sup.config.restart_min_minutes = sup.config.restart_max_minutes = 0.03  # 1.8 s
    await sup.start(target(1))
    await online(sup)
    sub = sup.hub.subscribe(1)  # a viewer, kept connected (drained below)
    drain = asyncio.create_task(_drain(sub))
    await asyncio.sleep(3.0)  # well past the planned time, rechecked every 0.3 s
    assert len(starts(fake_log)) == 1, "restarted although somebody was watching"
    assert sup.get_status()[1]["subscribers"] == 1
    sub.close()
    await drain
    assert await wait_until(lambda: len(starts(fake_log)) == 2, 3)
    assert not alive(starts(fake_log)[0][0])
    assert sup.hub.get(1).restarts >= 1


async def _drain(sub):
    async for _ in sub:
        pass


async def test_planned_restart_without_viewers(sup, fake_log):
    sup.config.restart_min_minutes = sup.config.restart_max_minutes = 0.02
    await sup.start(target(1))
    assert await wait_until(lambda: len(starts(fake_log)) == 2, 5)


async def test_planned_restart_time_is_random_per_camera(sup, fake_log):
    sup.config.restart_min_minutes, sup.config.restart_max_minutes = 30, 60
    for cam in (1, 2, 3, 4):
        await sup.start(target(cam))
    for cam in (1, 2, 3, 4):
        await online(sup, cam)
    planned = {round(s["next_planned_restart_in"]) for s in sup.get_status().values()}
    assert len(planned) > 1
    assert all(1800 - 5 <= p <= 3600 for p in planned)


async def test_shutdown_within_timeout_with_hung_children(sup, fake_log, tmp_path):
    sup.config.term_timeout_seconds = 30.0  # would take forever if not bounded
    sup.config.kill_timeout_seconds = 30.0
    for cam in (1, 2):
        await sup.start(target(cam, "hang"))
    for cam in (1, 2):
        await online(sup, cam)
    pids = [s["pid"] for s in sup.get_status().values()]
    t0 = time.monotonic()
    await sup.stop_all(timeout=1.0)
    assert time.monotonic() - t0 < 3.5
    assert await wait_until(lambda: not any(alive(p) for p in pids), 2)
    child = int((tmp_path / "child.pid").read_text())
    assert await wait_until(lambda: not alive(child), 2)
    assert sup.get_status() == {}


async def test_secret_never_logged_or_exposed(sup, fake_log, caplog):
    caplog.set_level(logging.DEBUG)
    await sup.start(target(1, "crash", frags=1))
    assert await wait_until(lambda: len(starts(fake_log)) >= 2)
    await sup.stop(1)
    assert "secret" not in caplog.text
    assert "secret" not in repr(sup.get_status())
    assert "secret" not in repr(target(1))
    assert "***" in sup.hub.get(1).last_error


async def test_missing_binary_is_reported_not_raised(sup, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "FFMPEG_BINARY", "/nonexistent/ffmpeg")
    await sup.start(target(1))
    assert await wait_until(lambda: sup.hub.get(1).last_error is not None)
    assert "cannot start" in sup.hub.get(1).last_error
    assert sup.is_running(1)


async def test_startup_reaps_orphan_of_previous_run(fake_bin, fake_log, cfg):
    from app.services.ingest.hub import IngestHub
    from app.services.ingest.supervisor import IngestSupervisor
    first = IngestSupervisor(hub=IngestHub(), config=cfg)
    await first.startup()
    await first.start(target(1))
    await online(first)
    pid = first.get_status()[1]["pid"]
    # simulate a crash of the app: the worker vanishes without killing the process
    async def no_kill(*a, **kw):
        return None

    first._terminate = no_kill
    first._registry.remove = lambda *a, **kw: None  # a crashed app cleans nothing up
    worker = first._workers.pop(1)
    worker.task.cancel()
    await asyncio.wait({worker.task}, timeout=2)
    assert alive(pid)
    _, entries = ow.Registry.load_previous(cfg.registry_path)
    assert [e.pid for e in entries] == [pid]  # registry still lists it
    try:
        second = IngestSupervisor(hub=IngestHub(), config=cfg)
        report = await second.startup()
        assert report["killed"] == [pid]
        assert await wait_until(lambda: not alive(pid), 2)
    finally:
        if alive(pid):
            os.killpg(pid, signal.SIGKILL)


def test_ffmpeg_args_composition(fake_bin, cfg):
    from app.services.ingest.hub import IngestHub
    from app.services.ingest.supervisor import IngestSupervisor
    sup = IngestSupervisor(hub=IngestHub(), config=cfg)
    args = sup._args("rtsp://u:p@h:554/x")
    assert "-timeout" in args and "-rw_timeout" not in args
    assert args[args.index("-timeout") + 1] == str(int(cfg.rtsp_timeout_seconds * 1_000_000))
    assert args[args.index("-c:v") + 1] == "copy"
    assert args[args.index("-c:a") + 1] == "aac"
    assert "-c" not in args
    assert "0:a:0?" in args
    assert args[args.index("-i") + 1] == "rtsp://u:p@h:554/x"
    assert args[-1] == "pipe:1"
