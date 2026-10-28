import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

from app.core.config import settings
from app.services.ingest.config import IngestConfig
from app.services.ingest.hub import IngestHub
from app.services.ingest.supervisor import IngestSupervisor, IngestTarget

FAKE = str(Path(__file__).resolve().parent.parent / "fake_ffmpeg.py")


@pytest.fixture
def fake_bin(monkeypatch):
    monkeypatch.setattr(settings, "FFMPEG_BINARY", FAKE)
    return FAKE


@pytest.fixture
def fake_log(tmp_path, monkeypatch):
    log = tmp_path / "starts.log"
    monkeypatch.setenv("FAKE_FFMPEG_LOG", str(log))
    monkeypatch.setenv("FAKE_FFMPEG_COUNTER", str(tmp_path / "counter"))
    monkeypatch.setenv("FAKE_FFMPEG_CHILD_PIDFILE", str(tmp_path / "child.pid"))
    return log


def starts(log: Path) -> list[tuple[int, float]]:
    if not log.exists():
        return []
    return [(int(a), float(b)) for a, b in (ln.split() for ln in log.read_text().splitlines())]


def alive(pid: int) -> bool:
    from app.services.ingest.ownership import pid_alive
    return pid_alive(pid)


async def wait_until(cond, timeout=8.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(step)
    return cond()


def target(cam_id=1, mode="ok", **query):
    q = "&".join(f"{k}={v}" for k, v in query.items())
    path = f"/{mode}" + (f"?{q}" if q else "")
    return IngestTarget(cam_id=cam_id, url=f"rtsp://user:secret@127.0.0.1:554{path}", name="t")


@pytest.fixture
def cfg(tmp_path):
    return IngestConfig(
        stall_seconds=1.0,
        restart_min_minutes=60.0,
        restart_max_minutes=60.0,
        backoff_max_seconds=0.8,
        backoff_initial_seconds=0.2,
        stable_seconds=0.5,
        restart_recheck_seconds=0.3,
        monitor_tick_seconds=0.05,
        term_timeout_seconds=0.5,
        kill_timeout_seconds=1.0,
        stop_timeout_seconds=4.0,
        registry_path=str(tmp_path / "ingest" / "registry.json"),
    )


@pytest_asyncio.fixture
async def sup(fake_bin, fake_log, cfg):
    s = IngestSupervisor(hub=IngestHub(max_buffer_bytes=1_000_000), config=cfg)
    await s.startup()
    yield s
    await s.stop_all(timeout=4.0)
    # safety net: whatever a failed test left behind
    for pid, _ in starts(fake_log):
        if alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
