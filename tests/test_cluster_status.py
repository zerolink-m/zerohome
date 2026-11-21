"""GET /v1/cluster and cluster.status.collect."""
import time

import pytest

from app.core.config import settings
from app.models.node import Assignment, Node
from app.services.cluster import runner as runner_mod
from app.services.cluster import status as status_mod
from app.services.cluster.runner import ClusterFunction
from tests.conftest import bearer, mint_test_token


async def tok(mk_user, email, role):
    uid = await mk_user(email, role=role)
    return await mint_test_token(uid, role)


def fn(name, wanted, scope="camera"):
    async def select():
        return dict(wanted)

    async def noop(*a):
        return True

    return ClusterFunction(name, select, noop, noop, scope=scope)


async def seed(sm, now):
    async with sm() as db:
        db.add_all([
            Node(node_id="a", started_at=now - 100, heartbeat_at=now - 3, capabilities={"ingest": True, "healthcheck": True},
                 address="10.0.0.1:8000", version="1"),
            Node(node_id="dead", started_at=now - 500, heartbeat_at=now - 400, capabilities={"motion": True},
                 address=None, version="1"),
            Assignment(function="ingest", resource_id=1, owner_node_id="a", generation=3, expires_at=now + 20, updated_at=now),
            Assignment(function="ingest", resource_id=2, owner_node_id="dead", generation=1, expires_at=now - 5, updated_at=now),
            Assignment(function="healthcheck", resource_id=0, owner_node_id=None, generation=2, expires_at=0, updated_at=now),
        ])
        await db.commit()


async def test_cluster_requires_admin(client, mk_user):
    assert (await client.get("/v1/cluster")).json()["code"] != "000"
    r = await client.get("/v1/cluster", headers=bearer(await tok(mk_user, "u@x.io", "user")))
    assert r.json()["code"] == "050"


async def test_disabled_cluster_returns_empty_lists(client, mk_user, monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", False)
    monkeypatch.setattr(status_mod, "get_node_id", lambda: "local-1")
    r = await client.get("/v1/cluster", headers=bearer(await tok(mk_user, "a@x.io", "admin")))
    assert r.status_code == 200 and r.json()["code"] == "000"
    c = r.json()["data"]["cluster"]
    assert c == {"node_id": "local-1", "cluster_enabled": False, "nodes": [], "assignments": [], "unassigned": []}


async def test_cluster_status_shape_and_unassigned(client, mk_user, sessionmaker_, monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)
    monkeypatch.setattr(status_mod, "get_node_id", lambda: "a")
    now = int(time.time())
    await seed(sessionmaker_, now)
    monkeypatch.setattr(runner_mod, "default_functions", lambda: [
        fn("ingest", {1: "rtsp://u:SECRETPASS@h/x", 2: "p", 3: "p"}),
        fn("motion", {1: "p"}),                   # nobody alive can do motion
        fn("healthcheck", {0: None}, "global"),   # lease released
        fn("tuya_events", {0: None}, "global"),   # disabled on every live node: not wanted
    ])
    from app.services.cluster import status
    monkeypatch.setattr(status, "default_functions", runner_mod.default_functions)
    r = await client.get("/v1/cluster", headers=bearer(await tok(mk_user, "a@x.io", "admin")))
    assert r.status_code == 200
    assert "SECRETPASS" not in r.text and "rtsp://" not in r.text
    c = r.json()["data"]["cluster"]
    assert c["node_id"] == "a" and c["cluster_enabled"] is True
    nodes = {n["node_id"]: n for n in c["nodes"]}
    assert nodes["a"]["alive"] is True and 0 <= nodes["a"]["heartbeat_age_seconds"] <= 10
    assert nodes["a"]["address"] == "10.0.0.1:8000" and nodes["a"]["capabilities"]["ingest"] is True
    assert nodes["a"]["version"] == "1"
    assert nodes["dead"]["alive"] is False and nodes["dead"]["heartbeat_age_seconds"] >= 399
    asg = {(a["function"], a["resource_id"]): a for a in c["assignments"]}
    live = asg[("ingest", 1)]
    assert live["owner_node_id"] == "a" and live["generation"] == 3 and live["expired"] is False
    assert 15 <= live["expires_in_seconds"] <= 20
    assert asg[("ingest", 2)]["expired"] is True and asg[("ingest", 2)]["expires_in_seconds"] == 0
    assert asg[("healthcheck", 0)]["owner_node_id"] is None and asg[("healthcheck", 0)]["expired"] is True
    un = {(u["function"], u["resource_id"]): u["reason"] for u in c["unassigned"]}
    assert un == {("ingest", 2): "unowned", ("ingest", 3): "unowned",
                  ("motion", 1): "no_capable_node", ("healthcheck", 0): "unowned"}


async def test_selection_failure_is_skipped(client, mk_user, monkeypatch):
    monkeypatch.setattr(settings, "CLUSTER_ENABLED", True)

    async def boom():
        raise RuntimeError("db")

    bad = ClusterFunction("ingest", boom, boom, boom)
    from app.services.cluster import status
    monkeypatch.setattr(status, "default_functions", lambda: [bad])
    r = await client.get("/v1/cluster", headers=bearer(await tok(mk_user, "a@x.io", "admin")))
    assert r.json()["code"] == "000" and r.json()["data"]["cluster"]["unassigned"] == []
