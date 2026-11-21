"""Lease logic over the `assignments` table (pure DB logic, no background tasks).

Every state change is a conditional UPDATE keyed on (function, resource_id) plus the
state we read (owner / generation / expires_at); rowcount == 1 means we won. Losers
re-read and decide again, so concurrent nodes can never both succeed.

generation: 1 when a row is created; +1 on every ownership change (including
re-acquiring after our own lease expired). Extending a live lease keeps it. release()
never lowers it. Time is always passed in as `now` (epoch seconds).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.node import Assignment, Node

_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class Lease:
    function: str
    resource_id: int
    owner: str
    generation: int
    expires_at: int
    holder: str | None = None  # process token; None only for callers that do not fence


async def register_node(db: AsyncSession, node_id: str, capabilities: dict, address: str | None,
                        version: str, now: int) -> None:
    """Create the node row or reset it (new process start)."""
    values = dict(started_at=now, heartbeat_at=now, capabilities=capabilities,
                  address=address or None, version=version)
    res = await db.execute(update(Node).where(Node.node_id == node_id).values(**values))
    if res.rowcount != 1:
        try:
            async with db.begin_nested():
                db.add(Node(node_id=node_id, **values))
        except IntegrityError:  # concurrent registration of the same id
            await db.execute(update(Node).where(Node.node_id == node_id).values(**values))
    await db.commit()


async def heartbeat(db: AsyncSession, node_id: str, capabilities: dict, address: str | None,
                    version: str, now: int) -> None:
    """Refresh heartbeat; re-creates the row if it was removed."""
    res = await db.execute(update(Node).where(Node.node_id == node_id).values(
        heartbeat_at=now, capabilities=capabilities, address=address or None, version=version))
    if res.rowcount != 1:
        await register_node(db, node_id, capabilities, address, version, now)
        return
    await db.commit()


async def try_acquire(db: AsyncSession, function: str, resource_id: int, node_id: str,
                      ttl: int, now: int, holder: str | None = None) -> Lease | None:
    """Take a free or expired lease.

    ``holder`` is this process's token. A live row owned by ``node_id`` but stamped
    with a different token belongs to another process: this one does not extend it
    and is not its owner. A token-less caller (None) keeps the old same-node extend.
    An expired row can be taken by anyone, including a restarted process.
    """
    key = and_(Assignment.function == function, Assignment.resource_id == resource_id)
    new_expiry = now + ttl
    for _ in range(_MAX_ATTEMPTS):
        row = (await db.execute(select(Assignment).where(key))).scalar_one_or_none()
        if row is None:
            try:
                async with db.begin_nested():
                    db.add(Assignment(function=function, resource_id=resource_id, owner_node_id=node_id,
                                      generation=1, expires_at=new_expiry, updated_at=now, holder=holder))
                await db.commit()
                return Lease(function, resource_id, node_id, 1, new_expiry, holder)
            except IntegrityError:
                await db.rollback()
                continue  # someone created it first: re-read
        # Snapshot values now; the read starts a transaction we must end.
        owner, gen, exp, row_holder = row.owner_node_id, row.generation, row.expires_at, row.holder
        await db.rollback()
        if owner == node_id and exp > now:
            if holder is not None and row_holder not in (None, holder):
                return None  # another live process with the same NODE_ID
            conds = [
                key, Assignment.owner_node_id == node_id, Assignment.generation == gen,
                Assignment.expires_at == exp,
            ]
            values: dict = dict(expires_at=new_expiry, updated_at=now)
            if holder is not None:
                values["holder"] = holder
                conds.append(Assignment.holder.is_(None) if row_holder is None else Assignment.holder == holder)
            res = await db.execute(update(Assignment).where(*conds).values(**values))
            new_gen = gen
        elif owner is None or exp <= now:
            owner_cond = Assignment.owner_node_id.is_(None) if owner is None else Assignment.owner_node_id == owner
            res = await db.execute(update(Assignment).where(
                key, owner_cond, Assignment.generation == gen, Assignment.expires_at == exp,
            ).values(owner_node_id=node_id, generation=gen + 1, expires_at=new_expiry,
                     updated_at=now, holder=holder))
            new_gen = gen + 1
        else:
            return None  # held by someone else
        if res.rowcount == 1:
            await db.commit()
            return Lease(function, resource_id, node_id, new_gen, new_expiry, holder)
        await db.rollback()  # lost the race: re-read
    return None


async def renew(db: AsyncSession, lease: Lease, ttl: int, now: int) -> bool:
    """Extend a live lease. A token on ``lease`` must still be the row's holder."""
    conds = [
        Assignment.function == lease.function, Assignment.resource_id == lease.resource_id,
        Assignment.owner_node_id == lease.owner, Assignment.generation == lease.generation,
        Assignment.expires_at > now,
    ]
    values: dict = dict(expires_at=now + ttl, updated_at=now)
    if lease.holder is not None:
        conds.append(Assignment.holder == lease.holder)
        values["holder"] = lease.holder
    res = await db.execute(update(Assignment).where(*conds).values(**values))
    if res.rowcount == 1:
        await db.commit()
        return True
    await db.rollback()
    return False


async def release(db: AsyncSession, lease: Lease) -> None:
    await db.execute(update(Assignment).where(
        Assignment.function == lease.function, Assignment.resource_id == lease.resource_id,
        Assignment.owner_node_id == lease.owner, Assignment.generation == lease.generation,
    ).values(owner_node_id=None, expires_at=0, holder=None))
    await db.commit()


async def list_assignments(db: AsyncSession) -> list[Assignment]:
    res = await db.execute(select(Assignment).order_by(Assignment.function, Assignment.resource_id))
    rows = list(res.scalars())
    return rows


async def list_nodes(db: AsyncSession, now: int, alive_within: int) -> list[Node]:
    """Nodes whose heartbeat is no older than `alive_within` seconds."""
    res = await db.execute(select(Node).where(Node.heartbeat_at >= now - alive_within).order_by(Node.node_id))
    rows = list(res.scalars())
    return rows
