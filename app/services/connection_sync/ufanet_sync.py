"""Synchronize independent Ufanet catalogs into connection_resources."""
from __future__ import annotations

import time

from sqlalchemy import select

from app.models.connection import Connection, ConnectionCredentials, ConnectionResource
from app.services.connection_sync.ufanet_cameras import materialize_ufanet_cameras
from app.services.connection_sync.ufanet_skuds import materialize_ufanet_skuds


async def _cameras(client):
    await client.login()
    result = []
    seen = set()
    page_number = 1
    while True:
        page = await client.get_cameras(page=page_number)
        if page.current != page_number or page.pages < page_number:
            raise ValueError('invalid camera pagination')
        for camera in page.results:
            if not camera.number or camera.number in seen:
                raise ValueError('duplicate or missing camera identifier')
            seen.add(camera.number)
            result.append((camera.number, camera.title or camera.address or camera.number,
                           {}, {'address': camera.address, 'permission': camera.permission}))
        if page_number == page.pages:
            if len(result) != page.count:
                raise ValueError('incomplete camera catalog')
            return result
        if not page.results or page_number >= 10000:
            raise ValueError('incomplete camera catalog')
        page_number += 1


async def _skuds(client):
    await client.login()
    items = await client.get_skuds()
    if not await client.is_authenticated():
        raise ValueError('Smart Home session expired')
    seen = set()
    result = []
    for item in items:
        if not item.id or item.id in seen:
            raise ValueError('duplicate or missing SKUD identifier')
        seen.add(item.id)
        result.append((item.id, item.address or item.id, {'open': item.shared},
                       {'address': item.address, 'has_camera': item.has_camera}))
    return result


async def sync_ufanet(db, connection_id: int) -> None:
    from ufanet_async import UfanetClient, UfanetConfig

    conn = (await db.execute(select(Connection).where(
        Connection.id == connection_id
    ).execution_options(populate_existing=True))).scalar_one_or_none()
    credentials = await db.get(ConnectionCredentials, connection_id, populate_existing=True)
    if conn is None or conn.provider != 'ufanet-smart-home' or not conn.enabled:
        return
    values = dict(credentials.credentials) if credentials else {}
    contract, password = values.get('contract'), values.get('password')
    if not isinstance(contract, str) or not contract or not isinstance(password, str) or not password:
        raise ValueError('Ufanet credentials are missing')

    snapshot = (conn.uid, conn.sync_revision)
    await db.rollback()

    results = {}
    errors = {}
    config = UfanetConfig(contract=contract, password=password)
    async with UfanetClient(config) as client:
        for name, fetch, service in (('camera', _cameras, client.ucams), ('skud', _skuds, client.smart_home)):
            try:
                results[name] = await fetch(service)
            except Exception as exc:
                errors[name] = type(exc).__name__

    conn = (await db.execute(select(Connection).where(
        Connection.id == connection_id
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    credentials = await db.get(ConnectionCredentials, connection_id, populate_existing=True)
    if (conn is None or conn.provider != 'ufanet-smart-home' or not conn.enabled
            or (conn.uid, conn.sync_revision) != snapshot
            or credentials is None or credentials.credentials != values):
        await db.rollback()
        return

    now = int(time.time())
    # A failed or ambiguous Smart Home catalog must fail closed.  Preserve the
    # resource rows and local settings so a later successful sync restores them.
    if 'skud' in errors or ('skud' in results and not results['skud']):
        resources = (await db.execute(select(ConnectionResource).where(
            ConnectionResource.connection_id == connection_id,
            ConnectionResource.resource_type == 'skud',
        ))).scalars().all()
        for resource in resources:
            resource.available = 0
        if 'skud' in results:
            errors['skud'] = 'AmbiguousEmptyCatalog'
    for kind, items in results.items():
        existing = (await db.execute(select(ConnectionResource).where(
            ConnectionResource.connection_id == connection_id,
            ConnectionResource.resource_type == kind,
        ))).scalars().all()
        by_id = {item.external_id: item for item in existing}
        seen = set()
        for external_id, name, capabilities, attributes in items:
            seen.add(external_id)
            resource = by_id.get(external_id)
            if resource is None:
                resource = ConnectionResource(connection_id=connection_id, resource_type=kind,
                                              external_id=external_id, first_seen_at=now)
                db.add(resource)
            resource.name = name[:255]
            resource.capabilities = capabilities
            resource.attributes = attributes
            resource.available = 1
            resource.last_seen_at = now
        for resource in existing:
            if resource.external_id not in seen:
                if kind == 'skud' and not items:
                    continue
                resource.available = 0
    await db.flush()
    if 'camera' in results:
        await materialize_ufanet_cameras(db, connection_id)
    if 'skud' in results:
        await materialize_ufanet_skuds(db, connection_id)
    conn.last_checked_at = now
    conn.sync_revision += 1
    conn.updated_at = now
    conn.last_sync_error = '; '.join(f'{name}: {error}' for name, error in errors.items()) or None
    conn.status = 'unavailable' if errors else 'online'
    if not errors:
        conn.last_synced_at = now
    await db.commit()
