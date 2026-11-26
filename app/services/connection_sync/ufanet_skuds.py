"""Materialize shared Ufanet access devices in the caller's transaction."""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection, ConnectionResource
from app.models.switch import Switch


async def materialize_ufanet_skuds(db: AsyncSession, connection_id: int) -> list[Switch]:
    connection = (await db.execute(
        select(Connection).where(Connection.id == connection_id).with_for_update()
    )).scalar_one_or_none()
    if connection is None or connection.provider != "ufanet-smart-home" or not connection.enabled:
        return []

    await db.flush()
    resources = (await db.execute(select(ConnectionResource).where(
        ConnectionResource.connection_id == connection_id,
        ConnectionResource.resource_type == "skud",
        ConnectionResource.available == 1,
    ))).scalars().all()
    switches = (await db.execute(select(Switch).where(
        Switch.connection_id == connection_id,
        Switch.added_by == "auto",
        Switch.is_master == 1,
    ))).scalars().all()
    by_external_id = {switch.external_id: switch for switch in switches}
    now = int(time.time())
    result = []
    for resource in resources:
        if (resource.capabilities or {}).get("open") is not True:
            continue
        switch = by_external_id.get(resource.external_id)
        if switch is None:
            switch = Switch(
                ownuser=connection.uid, name=resource.name, enabled=0, notify=0,
                control_proto="ufanet-smart-home", controlmeta={}, state=0,
                state_updated_at=now, added_by="auto", connection_id=connection_id,
                external_id=resource.external_id, is_master=1, parent=None,
                external_switch_channel=None, added_at=now, updated_at=now,
            )
            db.add(switch)
            by_external_id[resource.external_id] = switch
        result.append(switch)
    await db.flush()
    return result
