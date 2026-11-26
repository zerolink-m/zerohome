import pytest

from app.models.connection import Connection, ConnectionResource
from app.services.connection_sync.ufanet_cameras import materialize_ufanet_cameras
from tests.conftest import bearer, mint_test_token


@pytest.mark.asyncio
async def test_imported_camera_can_be_listed_enabled_and_viewed(client, mk_user, sessionmaker_, monkeypatch):
    owner = await mk_user('ucams-owner@example.org')
    async with sessionmaker_() as db:
        connection = Connection(uid=owner, provider='ufanet-smart-home', name='Home', meta={}, enabled=1, added_at=1, updated_at=1)
        db.add(connection)
        await db.flush()
        db.add(ConnectionResource(connection_id=connection.id, resource_type='camera', external_id='001', name='Camera', capabilities={}, attributes={}, available=1, first_seen_at=1, last_seen_at=1))
        await db.flush()
        camera = (await materialize_ufanet_cameras(db, connection.id))[0]
        camera_id = camera.id
        connection_id = connection.id
        assert camera.enabled == 0
        await db.commit()

    async def fake_saved(*args, **kwargs):
        pass
    monkeypatch.setattr('app.routers.cams.crud.ingest.on_cam_saved', fake_saved)
    headers = bearer(await mint_test_token(owner))
    response = await client.get(f'/v1/cams/{camera_id}', headers=headers)
    assert response.status_code == 200, response.text
    response = await client.patch(f'/v1/cams/{camera_id}', headers=headers, json={'data': {'cam': {'enabled': 1}}})
    assert response.status_code == 200, response.text
    response = await client.get('/v1/cams', headers=headers)
    assert response.status_code == 200, response.text
    assert any(cam['id'] == camera_id and cam['enabled'] == 1 for cam in response.json()['data']['cams'])
    async with sessionmaker_() as db:
        connection = await db.get(Connection, connection_id)
        connection.enabled = 0
        await db.commit()
    assert (await client.get(f'/v1/cams/{camera_id}', headers=headers)).status_code == 404
    async with sessionmaker_() as db:
        connection = await db.get(Connection, connection_id)
        connection.enabled = 1
        await db.commit()
    restored = await client.get(f'/v1/cams/{camera_id}', headers=headers)
    assert restored.status_code == 200
    assert restored.json()['data']['cam']['enabled'] == 1
