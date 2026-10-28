"""Every bucket name must map to exactly one (capacity, per_minute, cost-independent) config."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "app"
PAT = re.compile(r'(?:rate_limit|PointBudget)\(\s*"([a-z_]+)"\s*,\s*capacity=(\d+)\s*,\s*per_minute=([0-9.]+)')


def _collect():
    found = {}
    for f in ROOT.rglob("*.py"):
        for name, cap, pm in PAT.findall(f.read_text()):
            found.setdefault(name, set()).add((int(cap), float(pm)))
    return found


def test_no_bucket_name_with_conflicting_limits():
    clashes = {n: v for n, v in _collect().items() if len(v) > 1}
    assert not clashes, clashes


def test_split_buckets_and_generous_snapshot_limits():
    b = _collect()
    assert "cams_snapshot" not in b and "switches_state" not in b
    assert b["cams_snapshot_live"] == {(600, 600.0)}
    assert b["cams_recording_snapshot"] == {(3000, 3000.0)}
    assert b["switches_state_read"] == {(300, 300.0)}
    assert b["switches_state_write"] == {(30, 30.0)}
    assert b["cams_webrtc"] == {(20, 20.0)}
