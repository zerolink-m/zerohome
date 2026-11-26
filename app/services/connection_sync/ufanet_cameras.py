"""Materialize Ufanet camera resources within the caller's transaction."""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cams import Cam
from app.models.connection import Connection, ConnectionResource


async def materialize_ufanet_cameras(db: AsyncSession, connection_id: int) -> list[Cam]:
    """Create missing camera rows while preserving IDs and local settings.

    Call after the camera catalog has been flushed, before committing its
    transaction. The connection lock serializes concurrent materializations.
    """
    connection = (await db.execute(
        select(Connection).where(Connection.id == connection_id).with_for_update()
    )).scalar_one_or_none()
    if connection is None or connection.provider != "ufanet-smart-home" or not connection.enabled:
        return []

    await db.flush()
    resources = (await db.execute(
        select(ConnectionResource).where(
            ConnectionResource.connection_id == connection_id,
            ConnectionResource.resource_type == "camera",
            ConnectionResource.available == 1,
        )
    )).scalars().all()
    existing = (await db.execute(
        select(Cam).join(ConnectionResource, Cam.connection_resource_id == ConnectionResource.id)
        .where(ConnectionResource.connection_id == connection_id)
    )).scalars().all()
    by_resource = {camera.connection_resource_id: camera for camera in existing}
    now = int(time.time())
    cameras = []
    for resource in resources:
        camera = by_resource.get(resource.id)
        if camera is None:
            camera = Cam(
                ownuser=connection.uid, name=resource.name, enabled=0,
                connection_resource_id=resource.id, port=0, path="",
                username="", password="", added_at=now, updated_at=now,
            )
            db.add(camera)
        cameras.append(camera)
    await db.flush()
    return cameras
