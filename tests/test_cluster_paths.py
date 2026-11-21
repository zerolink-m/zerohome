"""Per-node registry/HLS directories in cluster mode."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

from app.core.config import PROJECT_ROOT, settings
from app.services.cluster import identity
from app.services.ingest import ownership as ow


def make(monkeypatch, *, cluster, node_id="", registry_dir="", hls_explicit=None):
    s = settings.model_copy()
    s.__pydantic_fields_set__.discard("HLS_TEMP_DIR")
    object.__setattr__(s, "CLUSTER_ENABLED", cluster)
    object.__setattr__(s, "NODE_ID", node_id)
    object.__setattr__(s, "INGEST_REGISTRY_DIR", registry_dir)
    object.__setattr__(s, "CACHE_DIR", "caching")
    object.__setattr__(s, "HLS_TEMP_DIR", "/tmp/zerocams/hls")
    if hls_explicit:
        s.__pydantic_fields_set__.add("HLS_TEMP_DIR")
        object.__setattr__(s, "HLS_TEMP_DIR", hls_explicit)
    return s


def test_disabled_paths_unchanged(monkeypatch):
    s = make(monkeypatch, cluster=False, node_id="a")
    assert s.ingest_registry_path == str(PROJECT_ROOT / "caching" / "ingest" / "registry.json")
    assert s.hls_temp_path == "/tmp/zerocams/hls"


def test_different_nodes_different_dirs(monkeypatch):
    a = make(monkeypatch, cluster=True, node_id="n1")
    b = make(monkeypatch, cluster=True, node_id="n2")
    assert a.ingest_registry_path != b.ingest_registry_path
    assert a.hls_temp_path != b.hls_temp_path
    assert a.ingest_registry_path == str(PROJECT_ROOT / "caching/ingest/nodes/n1/registry.json")
    assert a.hls_temp_path == "/tmp/zerocams/hls/nodes/n1"


def test_explicit_values_untouched(monkeypatch):
    s = make(monkeypatch, cluster=True, node_id="n1", registry_dir="/run/zc", hls_explicit="/data/hls")
    assert s.ingest_registry_path == "/run/zc/registry.json"
    assert s.hls_temp_path == "/data/hls"


def test_random_node_id_stable_in_process(monkeypatch):
    s = make(monkeypatch, cluster=True)
    assert s.ingest_registry_path == s.ingest_registry_path
    assert identity.get_node_id(s) in s.ingest_registry_path


@pytest.mark.parametrize("raw,expected", [
    ("node-1.a_b", "node-1.a_b"),
    ("a/b c", "a_b_c"),
    ("../etc", "__/etc".replace("/", "_")),
    ("..", "__"),
    (".", "node"),
    ("", "node"),
    ("узел", "____"),
])
def test_safe_node_dir_name(raw, expected):
    out = identity.safe_node_dir_name(raw)
    assert out == expected
    assert out and ".." not in out and "/" not in out


def test_hostile_node_id_stays_inside(monkeypatch):
    s = make(monkeypatch, cluster=True, node_id="../../x/..")
    p = Path(s.ingest_registry_path)
    assert (PROJECT_ROOT / "caching/ingest/nodes") in p.parents


async def test_registries_of_two_nodes_are_independent(monkeypatch, tmp_path):
    a = make(monkeypatch, cluster=True, node_id="n1", registry_dir="")
    b = make(monkeypatch, cluster=True, node_id="n2", registry_dir="")
    pa = tmp_path / Path(a.ingest_registry_path).relative_to(PROJECT_ROOT / "caching/ingest")
    pb = tmp_path / Path(b.ingest_registry_path).relative_to(PROJECT_ROOT / "caching/ingest")
    assert pa != pb
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
    try:
        ow.Registry(pa).add(1, proc.pid, 1)  # node n1 owns it
        report = await ow.reap_orphans(pb, binary="python")  # node n2 restarts
        assert report == {"killed": [], "gone": [], "foreign": []}
        assert proc.returncode is None
        os.kill(proc.pid, 0)  # still alive
    finally:
        proc.kill()
        await proc.wait()
