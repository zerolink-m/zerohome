"""Safe process ownership for ingest ffmpeg processes.

The server runs foreign ffmpeg processes against the same cameras (Emby, node
cameraThread). They must never be touched, so nothing here ever searches by process
name, camera IP or URL. A process may be signalled only when ALL of these hold:

  (a) our registry has an entry with its PID *and* its start time (/proc/<pid>/stat,
      field 22) — a reused PID has a different start time;
  (b) /proc/<pid>/cmdline contains our ffmpeg binary (FFMPEG_BINARY);
  (c) /proc/<pid>/environ carries ZEROCAMS_INGEST=<instance_id>:<cam_id> that matches
      the registry entry.

Anything unreadable or inconsistent means "not ours" (fail safe)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_TAG = "ZEROCAMS_INGEST"
_POLL_INTERVAL = 0.05


def make_tag(instance_id: str, cam_id: int) -> str:
    return f"{instance_id}:{cam_id}"


def new_instance_id() -> str:
    return secrets.token_hex(8)


# --- /proc readers (all return None on any problem) --------------------------------

def read_start_time(pid: int) -> int | None:
    """Process start time in clock ticks since boot (stat field 22), or None."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        # comm may contain spaces/parens: split after the LAST ')'
        fields = raw[raw.rindex(")") + 2:].split()
        return int(fields[19])  # field 22 overall; fields[0] is field 3 (state)
    except (OSError, ValueError, IndexError):
        return None


def _read_state(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        return raw[raw.rindex(")") + 2:].split()[0]
    except (OSError, ValueError, IndexError):
        return None


def read_cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except OSError:
        return None
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def read_environ(pid: int) -> dict[str, str] | None:
    try:
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
    except OSError:
        return None
    env: dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            env[key.decode(errors="replace")] = value.decode(errors="replace")
    return env


def pid_alive(pid: int) -> bool:
    """Alive and not a zombie."""
    state = _read_state(pid)
    return state is not None and state not in ("Z", "X")


def _cmdline_has_binary(cmdline: list[str], binary: str) -> bool:
    """`binary` is the FFMPEG_BINARY setting: a bare name ("ffmpeg") or a path. A script
    started through a shebang shows up as `<interpreter> <script> ...`, hence the
    small window of leading arguments."""
    if not binary or not cmdline:
        return False
    real = os.path.realpath(binary) if "/" in binary else None
    for arg in cmdline[:3]:
        if arg == binary:
            return True
        if real is not None and arg.startswith("/") and os.path.realpath(arg) == real:
            return True
        if real is None and os.path.basename(arg) == binary:
            return True
    return False


def is_ours(pid: int, *, start_time: int | None, binary: str, tag: str) -> bool:
    """True only when (a) start time, (b) binary in cmdline and (c) env tag all match."""
    if not isinstance(pid, int) or pid <= 1 or start_time is None or not tag:
        return False
    if read_start_time(pid) != start_time:  # (a) also False when the pid is gone
        return False
    if not pid_alive(pid):
        return False
    cmdline = read_cmdline(pid)
    if cmdline is None or not _cmdline_has_binary(cmdline, binary):  # (b)
        return False
    env = read_environ(pid)
    if env is None or env.get(ENV_TAG) != tag:  # (c)
        return False
    # re-check (a): the pid must not have changed hands while we were reading
    return read_start_time(pid) == start_time


# --- killing -----------------------------------------------------------------------

def _group_of(pid: int) -> int | None:
    """The process group to signal: only our own session leaders (pgid == pid, set by
    start_new_session=True). Never a group shared with someone else."""
    try:
        return pid if os.getpgid(pid) == pid else None
    except OSError:
        return None


def _signal(pid: int, pgid: int | None, sig: int) -> None:
    try:
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        logger.warning("ingest: no permission to signal pid %s", pid)


def _all_dead(pid: int, start_time: int | None, pgid: int | None) -> bool:
    leader_dead = not pid_alive(pid) or read_start_time(pid) != start_time
    return leader_dead and (pgid is None or not _group_has_live_member(pgid))


async def kill_tree_safely(
    pid: int,
    *,
    start_time: int | None,
    binary: str,
    tag: str,
    term_timeout: float = 3.0,
    kill_timeout: float = 2.0,
) -> str:
    """SIGTERM the process group, wait up to `term_timeout`, then SIGKILL the group.

    Returns "not_ours" (nothing sent), "gone" (already dead), "terminated" or "killed".
    A group leftover after the leader exited (e.g. a hung grandchild) is SIGKILLed too —
    the group id is the leader's pid, which the kernel cannot hand out while the group
    exists, so this cannot hit a stranger."""
    if not is_ours(pid, start_time=start_time, binary=binary, tag=tag):
        return "gone" if not pid_alive(pid) else "not_ours"
    pgid = _group_of(pid)
    _signal(pid, pgid, signal.SIGTERM)
    deadline = time.monotonic() + term_timeout
    while time.monotonic() < deadline:
        if _all_dead(pid, start_time, pgid):
            return "terminated"
        await asyncio.sleep(_POLL_INTERVAL)
    if _all_dead(pid, start_time, pgid):
        return "terminated"
    _signal(pid, pgid, signal.SIGKILL)
    deadline = time.monotonic() + kill_timeout
    while time.monotonic() < deadline and not _all_dead(pid, start_time, pgid):
        await asyncio.sleep(_POLL_INTERVAL)
    return "killed"


def _group_has_live_member(pgid: int) -> bool:
    """Any non-zombie process in group `pgid` (killpg(0) also succeeds for zombies)."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            raw = Path(f"/proc/{entry}/stat").read_text()
            fields = raw[raw.rindex(")") + 2:].split()
        except (OSError, ValueError):
            continue
        if len(fields) > 2 and fields[2] == str(pgid) and fields[0] not in ("Z", "X"):
            return True
    return False


def kill_now(pid: int, *, start_time: int | None, binary: str, tag: str) -> bool:
    """Synchronous SIGKILL of the group, no waiting (last resort when shutdown ran out
    of time). Same ownership checks. True when a signal was sent."""
    if not is_ours(pid, start_time=start_time, binary=binary, tag=tag):
        return False
    _signal(pid, _group_of(pid), signal.SIGKILL)
    return True


# --- registry ----------------------------------------------------------------------

@dataclass
class RegistryEntry:
    cam_id: int
    pid: int
    start_time: int


class Registry:
    """JSON file with this run's instance id and the processes it started.

        {"instance_id": "...", "entries": [{"cam_id": 1, "pid": 123, "start_time": 456}]}

    Written atomically. A damaged file is treated as empty (we then kill nothing)."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.instance_id: str = new_instance_id()
        self.entries: dict[int, RegistryEntry] = {}

    # reading the previous run
    @staticmethod
    def load_previous(path: str | os.PathLike) -> tuple[str | None, list[RegistryEntry]]:
        p = Path(path)
        try:
            data = json.loads(p.read_text())
            instance_id = data["instance_id"]
            if not isinstance(instance_id, str) or not instance_id:
                raise ValueError("bad instance_id")
            entries = [
                RegistryEntry(int(e["cam_id"]), int(e["pid"]), int(e["start_time"]))
                for e in data.get("entries", [])
            ]
            return instance_id, entries
        except FileNotFoundError:
            return None, []
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("ingest: registry %s is unreadable (%s); no orphans will be killed", p, exc)
            try:
                p.replace(p.with_name(p.name + ".corrupt"))
            except OSError:
                pass
            return None, []

    def add(self, cam_id: int, pid: int, start_time: int) -> None:
        self.entries[cam_id] = RegistryEntry(cam_id, pid, start_time)
        self.save()

    def remove(self, cam_id: int, pid: int | None = None) -> None:
        entry = self.entries.get(cam_id)
        if entry is not None and (pid is None or entry.pid == pid):
            del self.entries[cam_id]
            self.save()

    def save(self) -> None:
        payload = {
            "instance_id": self.instance_id,
            "entries": [vars(e) for e in self.entries.values()],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".registry-")
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.error("ingest: cannot write registry %s: %s", self.path, exc)


async def reap_orphans(
    path: str | os.PathLike,
    *,
    binary: str,
    term_timeout: float = 3.0,
) -> dict[str, list[int]]:
    """Kills leftovers of the previous run that pass all three ownership checks.

    Returns {"killed": [...pids], "gone": [...], "foreign": [...]}. Foreign (registry
    entry whose pid now belongs to something else) is logged and left alone."""
    result: dict[str, list[int]] = {"killed": [], "gone": [], "foreign": []}
    instance_id, entries = Registry.load_previous(path)
    if instance_id is None:
        return result
    for entry in entries:
        tag = make_tag(instance_id, entry.cam_id)
        if not pid_alive(entry.pid):
            result["gone"].append(entry.pid)
            continue
        outcome = await kill_tree_safely(
            entry.pid, start_time=entry.start_time, binary=binary, tag=tag, term_timeout=term_timeout
        )
        if outcome == "not_ours":
            logger.warning("ingest: pid %s (cam %s) is foreign, skipped", entry.pid, entry.cam_id)
            result["foreign"].append(entry.pid)
        elif outcome == "gone":
            result["gone"].append(entry.pid)
        else:
            logger.info("ingest: killed orphan pid %s (cam %s)", entry.pid, entry.cam_id)
            result["killed"].append(entry.pid)
    return result
