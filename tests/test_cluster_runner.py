"""Per-camera rights runner: fakes for start/stop, file SQLite for real concurrency."""
import asyncio
import importlib
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, settings
from app.db.base import Base
from app.models.node import Assignment
from app.services.cluster import leases, runner as runner_mod
from app.services.cluster.runner import ClusterFunction, ClusterRunner

main = importlib.import_module("app.main")


class Clock:
    def __init__(self, t=1000):
        self.t = t

    def __call__(self):
        return self.t


class Fake:
    """Stands in for the real ingest/motion start/stop entry points."""

    def __init__(self, name, cams, scope="camera", fingerprint=None):
        self.name, self.scope, self.fp = name, scope, fingerprint
        self.wanted = {c: f"payload{c}" for c in cams}
        self.running = set()
        self.starts, self.stops = [], []

    async def select(self):
        return dict(self.wanted)

    async def start(self, cam_id, payload):
        if cam_id not in self.running:
            self.starts.append(cam_id)
        self.running.add(cam_id)
        return True

    async def stop(self, cam_id):
        self.stops.append(cam_id)
        self.running.discard(cam_id)

    def function(self):
        return ClusterFunction(self.name, self.select, self.start, self.stop,
                               scope=self.scope, fingerprint=self.fp)


class Flaky:
    def __init__(self, sm):
        self.sm, self.down = sm, False

    def __call__(self):
        if self.down:
            raise OSError("db down")
        return self.sm()


@pytest_asyncio.fixture
async def sm(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}", connect_args={"timeout": 30})
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(eng, expire_on_commit=False)
    await eng.dispose()


def cfg(node, **kw):
    kw.setdefault("INGEST_ENABLED", True)
    return Settings(_env_file=None, CLUSTER_ENABLED=True, NODE_ID=node, **kw)


def make(node, sm, clock, fakes, **kw):
    return ClusterRunner(settings=cfg(node, **kw), sessionmaker=sm, clock=clock,
                         functions=[f.function() for f in fakes], node_id=node)


async def step(*runners):
    for r in runners:
        await r._heartbeat()
        await r.reconcile_once()
        await r.drain()


async def owners(sm, fn="ingest"):
    async with sm() as db:
        return {a.resource_id: a.owner_node_id for a in await leases.list_assignments(db) if a.function == fn}


async def test_single_node_takes_all(sm):
    f = Fake("ingest", [1, 2, 3])
    r = make("a", sm, Clock(), [f])
    await step(r)
    assert f.running == {1, 2, 3}
    assert await owners(sm) == {1: "a", 2: "a", 3: "a"}


async def test_two_nodes_split_fairly(sm):
    clock = Clock()
    fa, fb = Fake("ingest", [1, 2, 3, 4, 5]), Fake("ingest", [1, 2, 3, 4, 5])
    a, b = make("a", sm, clock, [fa]), make("b", sm, clock, [fb])
    await a._heartbeat(register=True)
    await b._heartbeat(register=True)
    await step(a, b)
    assert len(fa.running) == 3 and len(fb.running) == 2
    assert fa.running.isdisjoint(fb.running) and fa.running | fb.running == {1, 2, 3, 4, 5}
    clock.t += 10
    await step(a, b)  # stable: nothing moves
    assert len(fa.running) == 3 and len(fb.running) == 2 and not fa.stops and not fb.stops


async def test_survivor_takes_over_after_death(sm):
    clock = Clock()
    fa, fb = Fake("ingest", [1, 2, 3, 4]), Fake("ingest", [1, 2, 3, 4])
    a, b = make("a", sm, clock, [fa]), make("b", sm, clock, [fb])
    await a._heartbeat(register=True)
    await b._heartbeat(register=True)
    await step(a, b)
    assert len(fb.running) == 2
    for _ in range(4):  # a is dead: no more steps for it
        clock.t += 10
        await step(b)
    assert fb.running == {1, 2, 3, 4}
    assert set((await owners(sm)).values()) == {"b"}


async def test_graceful_stop_releases_and_other_takes(sm):
    clock = Clock()
    fa, fb = Fake("ingest", [1, 2]), Fake("ingest", [1, 2])
    a, b = make("a", sm, clock, [fa]), make("b", sm, clock, [fb])
    await a._heartbeat(register=True)
    await b._heartbeat(register=True)
    await step(a, b)
    await a.stop()
    assert set((await owners(sm)).values()) <= {"b", None}
    clock.t += 10
    await step(b)
    clock.t += 10
    await step(b)
    assert fb.running == {1, 2}


async def test_lost_right_stops_local_work(sm):
    clock = Clock()
    f = Fake("ingest", [1, 2])
    r = make("a", sm, clock, [f])
    await step(r)
    async with sm() as db:  # someone else took cam 1 (newer generation)
        await db.execute(update(Assignment).where(Assignment.resource_id == 1).values(
            owner_node_id="x", generation=Assignment.generation + 1, expires_at=clock.t + 100))
        await db.commit()
    clock.t += 10
    await step(r)
    assert f.running == {2} and f.stops == [1]
    assert r.held("ingest") == {2}


async def test_removed_or_disabled_camera_stops_and_releases(sm):
    clock = Clock()
    f = Fake("ingest", [1, 2])
    r = make("a", sm, clock, [f])
    await step(r)
    del f.wanted[2]
    clock.t += 10
    await step(r)
    assert f.running == {1} and f.stops == [2]
    assert (await owners(sm))[2] is None and (await owners(sm))[1] == "a"


async def test_db_outage_fences_and_recovery_reacquires(sm):
    clock = Clock()
    flaky = Flaky(sm)
    f = Fake("ingest", [1, 2])
    r = make("a", flaky, clock, [f])  # TTL 30, renew 10 -> fence at 20s without renewal
    await step(r)
    assert f.running == {1, 2}
    flaky.down = True
    clock.t += 10
    await step(r)
    assert f.running == {1, 2}  # 10s < threshold, still ours
    clock.t += 10
    await step(r)
    assert f.running == set() and r.held("ingest") == set()
    clock.t += 10
    await step(r)  # loop survives errors
    flaky.down = False
    clock.t += 10
    await step(r)
    assert f.running == {1, 2}
    assert set((await owners(sm)).values()) == {"a"}


async def test_capability_off_takes_nothing(sm):
    clock = Clock()
    fi = Fake("ingest", [1, 2])
    r = make("a", sm, clock, [fi], INGEST_ENABLED=False)
    await step(r)
    assert fi.running == set() and await owners(sm) == {}
    # the other node serves everything, and the incapable node does not count in the share
    fb = Fake("ingest", [1, 2])
    b = make("b", sm, clock, [fb])
    await r._heartbeat(register=True)
    await b._heartbeat(register=True)
    await step(b)
    assert fb.running == {1, 2}


async def test_capability_is_per_function(sm):
    clock = Clock()
    fi, fm = Fake("ingest", [1]), Fake("motion", [1])
    r = make("a", sm, clock, [fi, fm], MOTION_DETECTION_ENABLED=False)
    await step(r)
    assert fi.running == {1} and fm.running == set()
    assert await owners(sm, "motion") == {}


async def test_start_failure_is_retried(sm):
    clock = Clock()
    f = Fake("ingest", [1])
    calls = {"n": 0}
    real = f.start

    async def flaky_start(cam_id, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await real(cam_id, payload)

    f.start = flaky_start
    r = make("a", sm, clock, [f])
    await step(r)
    assert f.running == set()
    clock.t += 10
    await step(r)
    assert f.running == {1}


async def test_runner_start_is_noop_when_cluster_disabled(monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", False)
    monkeypatch.setattr(runner_mod, "_runner", None)
    ctor = Mock(side_effect=AssertionError("runner created"))
    monkeypatch.setattr(runner_mod, "ClusterRunner", ctor)
    await runner_mod.start()
    await runner_mod.stop()
    ctor.assert_not_called()


def _stub_lifespan(monkeypatch):
    monkeypatch.setattr(main, "start_cleanup_task", Mock())
    monkeypatch.setattr(main.session_activity, "start", Mock())
    monkeypatch.setattr(main.session_activity, "stop", AsyncMock())
    for flag in ("SESSION_CLEANUP_ENABLED", "HEALTHCHECK_ENABLED", "SWITCH_SYNC_ENABLED",
                 "CONNECTION_SYNC_ENABLED", "TUYA_EVENTS_ENABLED", "MOTION_CLEANUP_ENABLED",
                 "HLS_ENABLED", "WEBRTC_ENABLED", "STARTUP_WARMUP_ENABLED"):
        monkeypatch.setattr(settings, flag, False)
    for module, names in [
        (main.ingest, ("start_all_enabled", "stop_all")),
        (main.stream_manager, ("startup", "stop_all")),
        (main.hls_manager, ("startup", "stop_all")),
        (main.webrtc_manager, ("startup",)),
        (main.snapshot, ("startup", "stop_all")),
        (main.motion_detection, ("startup",)),
        (main.motion_lifecycle, ("start_all_enabled", "stop_all")),
        (main.tuya_events, ("stop_all",)),
        (main.tuya_client_pool, ("close_all",)),
        (main.motion_cleanup, ("stop",)),
        (main.auth, ("stop_session_cleanup",)),
        (main.cluster_runner, ("start", "stop")),
        (main.media_server, ("start", "stop")),
    ]:
        for name in names:
            monkeypatch.setattr(module, name, AsyncMock())


async def test_lifespan_legacy_path_when_cluster_disabled(monkeypatch):
    _stub_lifespan(monkeypatch)
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", False)
    async with main.lifespan(main.app):
        pass
    main.ingest.start_all_enabled.assert_awaited_once()
    main.motion_lifecycle.start_all_enabled.assert_awaited_once()
    main.cluster_runner.start.assert_not_awaited()
    main.media_server.start.assert_not_awaited()


async def test_lifespan_uses_runner_when_cluster_enabled(monkeypatch):
    _stub_lifespan(monkeypatch)
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)
    order = []
    main.media_server.stop.side_effect = lambda: order.append("media.stop")
    main.cluster_runner.stop.side_effect = lambda: order.append("runner.stop")
    main.motion_lifecycle.stop_all.side_effect = lambda: order.append("motion.stop_all")
    main.ingest.stop_all.side_effect = lambda: order.append("ingest.stop_all")
    async with main.lifespan(main.app):
        main.cluster_runner.start.assert_awaited_once()
        main.media_server.start.assert_awaited_once()
    main.ingest.start_all_enabled.assert_not_awaited()
    main.motion_lifecycle.start_all_enabled.assert_not_awaited()
    assert order == ["media.stop", "runner.stop", "motion.stop_all", "ingest.stop_all"]


# ---------------- global functions ----------------
def gfake(name="healthcheck"):
    return Fake(name, [0], scope="global")


async def test_global_function_taken_by_exactly_one_node(sm):
    clock = Clock()
    fa, fb = gfake(), gfake()
    a, b = make("a", sm, clock, [fa]), make("b", sm, clock, [fb])
    await a._heartbeat(register=True)
    await b._heartbeat(register=True)
    await step(a, b)
    assert (fa.running, fb.running) == ({0}, set())  # first one wins
    clock.t += 10
    await step(a, b)
    assert fa.starts == [0] and fb.starts == [] and fa.stops == []
    assert await owners(sm, "healthcheck") == {0: "a"}


async def test_global_function_taken_over_after_owner_death(sm):
    clock = Clock()
    fa, fb = gfake(), gfake()
    a, b = make("a", sm, clock, [fa]), make("b", sm, clock, [fb])
    await a._heartbeat(register=True)
    await b._heartbeat(register=True)
    await step(a, b)
    for _ in range(4):  # a is dead
        clock.t += 10
        await step(b)
    assert fb.running == {0} and await owners(sm, "healthcheck") == {0: "b"}


async def test_global_function_graceful_stop_hands_over(sm):
    clock = Clock()
    fa, fb = gfake(), gfake()
    a, b = make("a", sm, clock, [fa]), make("b", sm, clock, [fb])
    await step(a, b)
    await a.stop()
    assert fa.stops == [0] and fa.running == set()  # local loop stopped by the runner itself
    assert (await owners(sm, "healthcheck"))[0] is None
    clock.t += 10
    await step(b)
    assert fb.running == {0}


async def test_global_function_not_taken_without_capability(sm):
    clock = Clock()
    fa, fb = gfake(), gfake()
    a = make("a", sm, clock, [fa], HEALTHCHECK_ENABLED=False)
    b = make("b", sm, clock, [fb])
    await step(a)
    assert fa.running == set() and await owners(sm, "healthcheck") == {}
    await step(b)
    assert fb.running == {0}
    clock.t += 10
    await step(a, b)
    assert fa.running == set()


async def test_global_function_stopped_when_right_lost(sm):
    clock = Clock()
    f = gfake("switch_sync")
    r = make("a", sm, clock, [f])
    await step(r)
    assert f.running == {0}
    async with sm() as db:
        await db.execute(update(Assignment).where(Assignment.function == "switch_sync").values(
            owner_node_id="x", generation=Assignment.generation + 1, expires_at=clock.t + 100))
        await db.commit()
    clock.t += 10
    await step(r)
    assert f.running == set() and f.stops == [0] and r.held("switch_sync") == set()


async def test_global_function_fenced_on_db_outage(sm):
    clock = Clock()
    flaky = Flaky(sm)
    f = gfake("connection_sync")
    r = make("a", flaky, clock, [f])
    await step(r)
    flaky.down = True
    for _ in range(2):
        clock.t += 10
        await step(r)
    assert f.running == set() and r.held("connection_sync") == set()
    flaky.down = False
    clock.t += 10
    await step(r)
    assert f.running == {0}


async def test_default_global_functions_cover_the_four_and_skip_cleanups():
    names = {f.name: f.scope for f in runner_mod.default_functions()}
    assert names == {"ingest": "camera", "motion": "camera", "healthcheck": "global",
                     "switch_sync": "global", "connection_sync": "global", "tuya_events": "global"}


async def test_module_stop_functions_are_idempotent_without_start():
    from app.services import connection_sync, healthcheck, switch_sync, tuya_events
    for stop in (healthcheck.stop, switch_sync.stop, connection_sync.stop, tuya_events.stop_all):
        await stop()
        await stop()


async def test_module_stop_cancels_loop_and_allows_restart(monkeypatch):
    from app.services import connection_sync, healthcheck, switch_sync, tuya_events
    from app.services.connection_sync import runner as cs_runner
    for mod, getter in ((healthcheck, lambda: healthcheck._task), (switch_sync, lambda: switch_sync._task),
                        (cs_runner, lambda: cs_runner._task)):
        async def idle():
            await asyncio.sleep(60)
        monkeypatch.setattr(mod, "_loop", idle)
    healthcheck._last_online[1] = True
    for mod, stop in ((healthcheck, healthcheck.stop), (switch_sync, switch_sync.stop),
                      (cs_runner, connection_sync.stop)):
        mod.start()
        task = mod._task
        assert task is not None and not task.done()
        mod.start()
        assert mod._task is task  # idempotent
        await stop()
        assert task.cancelled() and mod._task is None
        mod.start()  # can be started again after a handover back
        assert mod._task is not task
        await stop()
    assert healthcheck._last_online == {}
    # tuya: stop_all cancels the supervisor task too
    async def idle2():
        await asyncio.sleep(60)
    monkeypatch.setattr(tuya_events, "_supervise", idle2)
    tuya_events.start()
    task = tuya_events._task
    await tuya_events.stop_all()
    assert task.cancelled() and tuya_events._task is None


# ---------------- camera changes reach the owner ----------------
async def test_changed_fingerprint_restarts_local_work_of_owner_only(sm):
    clock = Clock()
    fa = Fake("motion", [1, 2], fingerprint=lambda p: p)
    fb = Fake("motion", [1, 2], fingerprint=lambda p: p)
    a, b = make("a", sm, clock, [fa]), make("b", sm, clock, [fb])
    await a._heartbeat(register=True)
    await b._heartbeat(register=True)
    await step(a, b)
    assert fa.running == {1} and fb.running == {2}
    clock.t += 10
    await step(a, b)
    assert fa.stops == [] and fb.stops == []  # unchanged: nothing restarts
    for f in (fa, fb):  # camera 1 edited on whichever node accepted the request
        f.wanted[1] = "changed"
    clock.t += 10
    await step(a, b)
    assert fa.stops == [1] and fa.starts == [1, 1] and fa.running == {1}
    assert fb.stops == [] and fb.running == {2}
    assert r_held(a) == {1}


def r_held(r):
    return r.held("motion")


async def test_deleted_camera_stops_on_owner_while_other_node_sees_it_gone(sm):
    clock = Clock()
    fa, fb = Fake("ingest", [1, 2]), Fake("ingest", [1, 2])
    a, b = make("a", sm, clock, [fa]), make("b", sm, clock, [fb])
    await a._heartbeat(register=True)
    await b._heartbeat(register=True)
    await step(a, b)
    assert fa.running == {1} and fb.running == {2}
    for f in (fa, fb):
        f.wanted.pop(1)  # deleted through some node
    clock.t += 10
    await step(a, b)
    assert fa.running == set() and fa.stops == [1] and fb.running == {2} and fb.stops == []
    assert (await owners(sm))[1] is None


def test_cam_fingerprint_changes_with_camera_fields_and_hides_password():
    from types import SimpleNamespace
    base = dict(enabled=1, name="n", proto="rtsp", addr="h", port=554, path="p", username="u", password="secret")
    a = runner_mod.cam_fingerprint(SimpleNamespace(**base))
    assert a == runner_mod.cam_fingerprint(SimpleNamespace(**base))
    assert "secret" not in a
    for k, v in dict(addr="h2", path="q", password="x", name="m", enabled=0, port=555).items():
        assert runner_mod.cam_fingerprint(SimpleNamespace(**{**base, k: v})) != a


async def test_request_reconcile_wakes_loop_and_is_noop_when_stopped(sm):
    f = Fake("ingest", [1])
    r = ClusterRunner(settings=cfg("a", LEASE_RENEW_INTERVAL_SECONDS=10, LEASE_TTL_SECONDS=30),
                      sessionmaker=sm, functions=[f.function()], node_id="a")
    r.request_reconcile()
    assert not r._wake.is_set()  # not running: nothing happens
    runner_mod.request_reconcile()  # module-level: no runner, no error
    await r.start()
    try:
        await r.drain()
        assert f.running == {1}
        f.wanted[2] = "p"
        r.request_reconcile()
        for _ in range(60):
            await asyncio.sleep(0.1)
            await r.drain()
            if 2 in f.running:
                break
        assert f.running == {1, 2}  # well before the 10 s interval
    finally:
        await r.stop()


# ---------------- hooks ----------------
async def test_hooks_in_cluster_mode_only_request_reconcile(monkeypatch):
    from app.services.ingest import lifecycle
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)
    monkeypatch.setattr(settings, "INGEST_ENABLED", True)
    sup = Mock(side_effect=AssertionError("supervisor touched"))
    monkeypatch.setattr(lifecycle, "get_supervisor", sup)
    rr = Mock()
    monkeypatch.setattr(runner_mod, "request_reconcile", rr)
    cam = type("C", (), {"id": 1})()
    await lifecycle.on_cam_saved(cam)
    await lifecycle.on_cam_saved(cam, {"address"})
    assert rr.call_count == 2
    await lifecycle.on_cam_saved(cam, {"name"})  # irrelevant for ingest/motion
    assert rr.call_count == 2
    await lifecycle.on_cam_deleted(1)
    assert rr.call_count == 3
    sup.assert_not_called()


async def test_hooks_in_cluster_mode_work_with_ingest_flag_off(monkeypatch):
    from app.services.ingest import lifecycle
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)
    monkeypatch.setattr(settings, "INGEST_ENABLED", False)  # motion-only node still wakes the runner
    rr = Mock()
    monkeypatch.setattr(runner_mod, "request_reconcile", rr)
    await lifecycle.on_cam_deleted(5)
    rr.assert_called_once()


async def test_hooks_without_cluster_apply_locally(monkeypatch):
    from app.services.ingest import lifecycle
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", False)
    monkeypatch.setattr(settings, "INGEST_ENABLED", True)
    sup = Mock()
    sup.apply = AsyncMock()
    sup.stop = AsyncMock()
    monkeypatch.setattr(lifecycle, "get_supervisor", lambda: sup)
    rr = Mock()
    monkeypatch.setattr(runner_mod, "request_reconcile", rr)
    cam = type("C", (), {"id": 1, "enabled": 0, "addr": "h"})()
    await lifecycle.on_cam_saved(cam)
    sup.apply.assert_awaited_once_with(1, None)
    await lifecycle.on_cam_deleted(1)
    sup.stop.assert_awaited_once_with(1)
    rr.assert_not_called()


# ---------------- lifespan: the four global tasks ----------------
async def test_lifespan_does_not_start_global_tasks_in_cluster_mode(monkeypatch):
    _stub_lifespan(monkeypatch)
    starts = {}
    for mod in (main.healthcheck, main.switch_sync, main.connection_sync, main.tuya_events):
        starts[mod.__name__] = Mock()
        monkeypatch.setattr(mod, "start", starts[mod.__name__])
    for flag in ("HEALTHCHECK_ENABLED", "SWITCH_SYNC_ENABLED", "CONNECTION_SYNC_ENABLED", "TUYA_EVENTS_ENABLED"):
        monkeypatch.setattr(settings, flag, True)
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)
    async with main.lifespan(main.app):
        pass
    assert all(m.call_count == 0 for m in starts.values())
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", False)
    async with main.lifespan(main.app):
        pass
    assert all(m.call_count == 1 for m in starts.values())


# ---------------- the seven lease/worker fixes ----------------
class _ShiftSession:
    """Opens the real session, optionally moving the clock first (a slow call)."""

    def __init__(self, sm, clock):
        self.sm = sm
        self.clock = clock
        self.shift = 0

    def __call__(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                if outer.shift:
                    outer.clock.t += outer.shift
                self.inner = outer.sm()
                return await self.inner.__aenter__()

            async def __aexit__(self, *exc):
                return await self.inner.__aexit__(*exc)

        return _Ctx()


class _BlockSession:
    """Holds the session open until released, after jumping the clock past the fence."""

    def __init__(self, sm, clock):
        self.sm = sm
        self.clock = clock
        self.block = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def __call__(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                if outer.block:
                    outer.clock.t += 30
                    outer.entered.set()
                    await outer.release.wait()
                self.inner = outer.sm()
                return await self.inner.__aenter__()

            async def __aexit__(self, *exc):
                return await self.inner.__aexit__(*exc)

        return _Ctx()


async def test_stop_releases_only_after_local_work_has_stopped(sm):
    clock = Clock()
    fi = Fake("ingest", [1])
    fg = Fake("healthcheck", [0], scope="global")
    seen = []

    def watch(fake, function):
        real = fake.stop

        async def stop(cam_id, real=real, function=function):
            seen.append(function)
            owner = (await owners(sm, function)).get(cam_id)
            assert owner == "a"  # right still ours while this stop runs
            assert cam_id in fake.running
            await real(cam_id)

        fake.stop = stop

    watch(fi, "ingest")
    watch(fg, "healthcheck")
    runner = make("a", sm, clock, [fi, fg])
    await step(runner)
    assert fi.running == {1} and fg.running == {0}
    await runner.stop()
    assert seen == ["ingest", "healthcheck"] or set(seen) == {"ingest", "healthcheck"}
    assert fi.running == set() and fg.running == set()
    assert (await owners(sm, "ingest")).get(1) is None
    assert (await owners(sm, "healthcheck")).get(0) is None


async def test_renewed_at_is_the_time_the_renew_succeeded(sm):
    clock = Clock()
    shift = _ShiftSession(sm, clock)
    fake = Fake("ingest", [1])
    runner = make("a", shift, clock, [fake])
    await step(runner)
    assert runner._held[("ingest", 1)].renewed_at == 1000
    clock.t += 10  # 1010, still inside the fence margin
    shift.shift = 4  # the renew call itself moves the clock
    await runner.reconcile_once()
    await runner.drain()
    assert clock.t == 1014
    assert runner._held[("ingest", 1)].renewed_at == 1014
    assert fake.running == {1}


async def test_fence_stops_work_while_a_db_call_is_still_in_flight(sm):
    clock = Clock()
    blocker = _BlockSession(sm, clock)
    fake = Fake("ingest", [1])
    runner = make("a", blocker, clock, [fake])
    runner._fence_poll_seconds = 0.05
    await step(runner)
    assert fake.running == {1}
    clock.t += 10
    blocker.block = True
    task = asyncio.create_task(runner.reconcile_once())
    try:
        await asyncio.wait_for(blocker.entered.wait(), 2)
        for _ in range(40):
            if not fake.running:
                break
            await asyncio.sleep(0.05)
        assert fake.running == set() and fake.stops == [1]
        assert runner.held("ingest") == set()
    finally:
        blocker.release.set()
        await task


async def test_second_process_with_same_node_id_cannot_renew(sm):
    from dataclasses import replace

    clock = Clock()
    fa, fb = Fake("ingest", [1]), Fake("ingest", [1])
    a, b = make("same", sm, clock, [fa]), make("same", sm, clock, [fb])
    assert a.holder != b.holder
    await step(a)
    clock.t += 10
    await step(a, b)
    assert fa.running == {1} and fb.running == set() and b.held("ingest") == set()
    async with sm() as db:
        row = (await leases.list_assignments(db))[0]
        assert row.holder == a.holder and row.generation == 1 and row.owner_node_id == "same"
    foreign = replace(a._held[("ingest", 1)].lease, holder=b.holder)
    async with sm() as db:
        assert await leases.renew(db, foreign, 30, clock.t) is False
        assert await leases.renew(db, a._held[("ingest", 1)].lease, 30, clock.t) is True
    # Old process is dead and the lease has expired: a restart takes it, as before.
    clock.t += 40
    await step(b)
    assert fb.running == {1}
    async with sm() as db:
        row = (await leases.list_assignments(db))[0]
        assert row.owner_node_id == "same" and row.generation == 2 and row.holder == b.holder


async def test_apply_restarts_a_worker_whose_task_died(tmp_path):
    from app.services.ingest.config import IngestConfig
    from app.services.ingest.hub import IngestHub
    from app.services.ingest.supervisor import IngestSupervisor, IngestTarget

    sup = IngestSupervisor(
        hub=IngestHub(),
        config=IngestConfig(registry_path=str(tmp_path / "reg.json"), backoff_initial_seconds=30),
    )
    target = IngestTarget(1, "rtsp://u:pw@10.0.0.5/live", "cam")
    crashes = {"n": 0}

    async def crash_once(worker):
        crashes["n"] += 1
        if crashes["n"] == 1:
            raise RuntimeError("worker crashed")
        await asyncio.sleep(30)

    sup._run = crash_once
    try:
        assert await sup.apply(1, target) == "started"
        for _ in range(50):
            task = sup._workers[1].task
            if task is not None and task.done():
                break
            await asyncio.sleep(0.01)
        assert sup._workers[1].task.done()
        assert await sup.apply(1, target) == "restarting"
        await asyncio.sleep(0)  # let the replacement task enter _run
        assert crashes["n"] == 2
        assert not sup._workers[1].task.done()
    finally:
        await sup.stop(1)


async def test_dead_motion_session_is_started_again():
    from app.services.motion_detection import manager as md
    from app.services.motion_detection.session import MotionDetectionSession

    ran = []
    release = asyncio.Event()

    async def fake_run(self):
        ran.append(1)
        await release.wait()
        self.is_running = False  # what _run's finally does after retries are exhausted

    session = MotionDetectionSession(type("C", (), {"id": 7})(), [])
    session.is_running = False
    session.task = None
    md._active_sessions[7] = session
    original = MotionDetectionSession._run
    MotionDetectionSession._run = fake_run
    try:
        assert await md.start_motion_detection(7) is True
        await asyncio.sleep(0)
        assert ran == [1] and session.is_running
        assert await md.start_motion_detection(7) is True
        assert ran == [1]  # still alive: not started a second time
        first = session.task
        release.set()
        await asyncio.wait({first}, timeout=1)
        assert session.is_running is False and first.done()
        release.clear()
        assert await md.start_motion_detection(7) is True
        await asyncio.sleep(0)
        assert ran == [1, 1] and session.is_running
    finally:
        MotionDetectionSession._run = original
        md._active_sessions.pop(7, None)
        release.set()
        if session.task is not None and not session.task.done():
            session.task.cancel()
            await asyncio.wait({session.task}, timeout=1)
