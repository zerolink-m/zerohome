import json
import os
import signal
import subprocess
import sys
import time

import pytest

from app.services.ingest import ownership as ow
from tests.ingest.conftest import FAKE

URL = "rtsp://user:secret@127.0.0.1:554/ok"
HANG_URL = "rtsp://user:secret@127.0.0.1:554/hang"


def spawn(url=URL, tag=None, binary=FAKE, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k != ow.ENV_TAG}
    if tag:
        env[ow.ENV_TAG] = tag
    env.update(extra_env or {})
    proc = subprocess.Popen(
        [binary, "-i", url, "pipe:1"], env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # The script starts through `#!/usr/bin/env python3`: until env has exec'd python,
    # cmdline is [env, python3, script, ...] (it already contains the script path), after
    # it [python3, script, ...]. Wait for the final form so cmdline reads are stable.
    cmd = []
    for _ in range(500):  # up to ~5 s on a slow CI machine
        cmd = ow.read_cmdline(proc.pid) or []
        if cmd and os.path.basename(cmd[0]) != "env" and any(binary == a for a in cmd):
            break
        time.sleep(0.01)
    else:
        proc.kill()
        proc.wait()
        pytest.fail(f"spawned process did not finish exec within 5 s, cmdline={cmd!r}")
    return proc


@pytest.fixture
def procs():
    made = []

    def _spawn(*a, **kw):
        p = spawn(*a, **kw)
        made.append(p)
        return p

    yield _spawn
    for p in made:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.wait()


def test_read_start_time_and_dead_pid():
    assert isinstance(ow.read_start_time(os.getpid()), int)
    assert ow.read_start_time(2 ** 22 + 12345) is None


def test_is_ours_requires_all_three(procs):
    tag = ow.make_tag("inst", 5)
    p = procs(tag=tag)
    st = ow.read_start_time(p.pid)
    assert ow.is_ours(p.pid, start_time=st, binary=FAKE, tag=tag)
    # (a) start time differs (pid reuse)
    assert not ow.is_ours(p.pid, start_time=st + 1, binary=FAKE, tag=tag)
    assert not ow.is_ours(p.pid, start_time=None, binary=FAKE, tag=tag)
    # (b) other binary
    assert not ow.is_ours(p.pid, start_time=st, binary="/usr/bin/ffmpeg", tag=tag)
    assert not ow.is_ours(p.pid, start_time=st, binary="ffmpeg", tag=tag)
    # (c) other instance / other camera / no tag
    assert not ow.is_ours(p.pid, start_time=st, binary=FAKE, tag=ow.make_tag("other", 5))
    assert not ow.is_ours(p.pid, start_time=st, binary=FAKE, tag=ow.make_tag("inst", 6))
    assert not ow.is_ours(p.pid, start_time=st, binary=FAKE, tag="")
    # never pid 0/1/negative
    assert not ow.is_ours(1, start_time=1, binary=FAKE, tag=tag)
    assert not ow.is_ours(-5, start_time=1, binary=FAKE, tag=tag)


async def test_foreign_process_same_cmdline_same_url_is_not_killed(procs):
    tag = ow.make_tag("inst", 5)
    ours = procs(tag=tag)
    foreign = procs()  # identical command line and URL, no tag (Emby-like)
    assert ow.read_cmdline(ours.pid) == ow.read_cmdline(foreign.pid)
    res = await ow.kill_tree_safely(
        foreign.pid, start_time=ow.read_start_time(foreign.pid), binary=FAKE, tag=tag, term_timeout=0.3
    )
    assert res == "not_ours" and foreign.poll() is None
    # a foreign process carrying a stranger's tag is equally safe
    other = procs(tag=ow.make_tag("someone-else", 5))
    res = await ow.kill_tree_safely(
        other.pid, start_time=ow.read_start_time(other.pid), binary=FAKE, tag=tag, term_timeout=0.3
    )
    assert res == "not_ours" and other.poll() is None
    assert ours.poll() is None


async def test_reused_pid_wrong_start_time_not_killed(procs):
    tag = ow.make_tag("inst", 5)
    p = procs(tag=tag)
    st = ow.read_start_time(p.pid)
    res = await ow.kill_tree_safely(p.pid, start_time=st - 100, binary=FAKE, tag=tag, term_timeout=0.3)
    assert res == "not_ours" and p.poll() is None


async def test_kill_tree_safely_terminates_ours(procs):
    tag = ow.make_tag("inst", 5)
    p = procs(tag=tag)
    t0 = time.monotonic()
    res = await ow.kill_tree_safely(
        p.pid, start_time=ow.read_start_time(p.pid), binary=FAKE, tag=tag, term_timeout=2.0
    )
    assert res == "terminated" and time.monotonic() - t0 < 1.5
    assert p.wait(2) == -signal.SIGTERM


async def test_kill_tree_safely_escalates_and_kills_group(procs, tmp_path):
    tag = ow.make_tag("inst", 5)
    pidfile = tmp_path / "child.pid"
    p = procs(url=HANG_URL, tag=tag, extra_env={"FAKE_FFMPEG_CHILD_PIDFILE": str(pidfile)})
    for _ in range(300):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.01)
    child = int(pidfile.read_text())
    assert ow.pid_alive(child)
    res = await ow.kill_tree_safely(
        p.pid, start_time=ow.read_start_time(p.pid), binary=FAKE, tag=tag,
        term_timeout=0.3, kill_timeout=2.0,
    )
    assert res == "killed"
    p.wait(2)
    deadline = time.monotonic() + 2
    while ow.pid_alive(child) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not ow.pid_alive(child)


async def test_kill_tree_gone_process(procs):
    tag = ow.make_tag("inst", 5)
    p = procs(tag=tag)
    st = ow.read_start_time(p.pid)
    os.killpg(p.pid, signal.SIGKILL)
    p.wait()
    assert await ow.kill_tree_safely(p.pid, start_time=st, binary=FAKE, tag=tag) == "gone"


def write_registry(path, instance_id, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"instance_id": instance_id, "entries": entries}))


async def test_reap_orphans_kills_ours_skips_foreign(procs, tmp_path):
    reg = tmp_path / "r" / "registry.json"
    orphan = procs(tag=ow.make_tag("prev", 1))
    foreign = procs()  # registered pid, but no tag: pid "reused" by a stranger
    reused = procs(tag=ow.make_tag("prev", 3))  # tag right, start time wrong
    write_registry(reg, "prev", [
        {"cam_id": 1, "pid": orphan.pid, "start_time": ow.read_start_time(orphan.pid)},
        {"cam_id": 2, "pid": foreign.pid, "start_time": ow.read_start_time(foreign.pid)},
        {"cam_id": 3, "pid": reused.pid, "start_time": ow.read_start_time(reused.pid) + 5},
        {"cam_id": 4, "pid": 2 ** 22 + 99, "start_time": 1},
    ])
    res = await ow.reap_orphans(reg, binary=FAKE, term_timeout=1.0)
    assert res["killed"] == [orphan.pid]
    assert sorted(res["foreign"]) == sorted([foreign.pid, reused.pid])
    assert res["gone"] == [2 ** 22 + 99]
    orphan.wait(2)
    assert foreign.poll() is None and reused.poll() is None


@pytest.mark.parametrize("content", ["{not json", "[]", '{"instance_id": 5}', '{"instance_id":"x","entries":[{"cam_id":1}]}', ""])
async def test_reap_orphans_corrupt_registry_kills_nothing(procs, tmp_path, content):
    reg = tmp_path / "registry.json"
    p = procs(tag=ow.make_tag("prev", 1))
    reg.write_text(content)
    res = await ow.reap_orphans(reg, binary=FAKE, term_timeout=0.3)
    assert res == {"killed": [], "gone": [], "foreign": []}
    assert p.poll() is None
    assert not reg.exists() and (tmp_path / "registry.json.corrupt").exists()


async def test_reap_orphans_missing_registry(tmp_path):
    res = await ow.reap_orphans(tmp_path / "none" / "registry.json", binary=FAKE)
    assert res == {"killed": [], "gone": [], "foreign": []}


def test_registry_roundtrip_and_atomic_write(tmp_path):
    path = tmp_path / "d" / "registry.json"
    reg = ow.Registry(path)
    reg.add(1, 111, 222)
    reg.add(2, 333, 444)
    reg.remove(1, pid=999)  # wrong pid: kept
    assert 1 in reg.entries
    reg.remove(1, pid=111)
    iid, entries = ow.Registry.load_previous(path)
    assert iid == reg.instance_id
    assert [(e.cam_id, e.pid, e.start_time) for e in entries] == [(2, 333, 444)]
    assert [p.name for p in path.parent.iterdir()] == ["registry.json"]  # no temp leftovers
