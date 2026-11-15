"""Read-only view of the cluster for GET /v1/cluster (no secrets: only ids, counters, flags)."""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select

from app.models.node import Node
from app.services.cluster import leases
from app.services.cluster.identity import get_node_id
from app.services.cluster.runner import ClusterFunction, default_functions

logger = logging.getLogger(__name__)


async def collect(db, settings: Any, functions: list[ClusterFunction] | None = None,
                  now: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "node_id": get_node_id(settings),
        "cluster_enabled": bool(settings.CLUSTER_ENABLED),
        "nodes": [],
        "assignments": [],
        "unassigned": [],
    }
    if not settings.CLUSTER_ENABLED:
        return result
    now = int(time.time()) if now is None else now
    alive_within = 2 * int(settings.NODE_HEARTBEAT_INTERVAL_SECONDS)

    nodes = (await db.execute(select(Node).order_by(Node.node_id))).scalars().all()
    rows = await leases.list_assignments(db)

    alive_caps: list[dict] = []
    for n in nodes:
        age = max(0, now - int(n.heartbeat_at))
        alive = age <= alive_within
        caps = dict(n.capabilities or {})
        if alive:
            alive_caps.append(caps)
        result["nodes"].append({
            "node_id": n.node_id, "address": n.address, "heartbeat_age_seconds": age,
            "alive": alive, "capabilities": caps, "version": n.version,
        })

    live: set[tuple[str, int]] = set()
    for a in rows:
        expires_in = int(a.expires_at) - now
        expired = a.owner_node_id is None or expires_in <= 0
        if not expired:
            live.add((a.function, a.resource_id))
        result["assignments"].append({
            "function": a.function, "resource_id": a.resource_id, "owner_node_id": a.owner_node_id,
            "generation": a.generation, "expires_in_seconds": max(0, expires_in), "expired": expired,
        })

    for f in functions if functions is not None else default_functions():
        capable = any(c.get(f.name) for c in alive_caps)
        if f.scope == "global" and not capable:
            continue  # nobody is supposed to run it (disabled everywhere): not "wanted"
        try:
            desired = await f.select()
        except Exception:
            logger.warning("cluster status: selection of %s failed", f.name, exc_info=True)
            continue
        for rid in sorted(desired):
            if (f.name, rid) not in live:
                result["unassigned"].append({
                    "function": f.name, "resource_id": rid,
                    "reason": "unowned" if capable else "no_capable_node",
                })
    return result
