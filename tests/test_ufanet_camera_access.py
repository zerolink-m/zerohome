import pytest

from app.core.codes import (
    CLOUD_CAMERA_REQUIRES_HLS, IMPORTED_CAMERA_DELETE_VIA_CONNECTION, IMPORTED_CAMERA_READ_ONLY,
)
from app.models.connection import Connection, ConnectionResource
from app.services.connection_sync.ufanet_cameras import materialize_ufanet_cameras
from tests.conftest import bearer, mint_test_token


@pytest.mark.asyncio
async def test_imported_camera_is_enabled_and_read_only(client, mk_user, sessionmaker_):
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
        assert camera.enabled == 1
        await db.commit()

    headers = bearer(await mint_test_token(owner))
    response = await client.get(f'/v1/cams/{camera_id}', headers=headers)
    assert response.status_code == 200, response.text
    response = await client.patch(f'/v1/cams/{camera_id}', headers=headers, json={'data': {'cam': {'enabled': 0}}})
    assert response.status_code == 400 and response.json()['code'] == IMPORTED_CAMERA_READ_ONLY == '399', response.text
    response = await client.delete(f'/v1/cams/{camera_id}', headers=headers)
    assert response.status_code == 400 and response.json()['code'] == IMPORTED_CAMERA_DELETE_VIA_CONNECTION == '400', response.text
    response = await client.get(f'/v1/cams/{camera_id}/stream', headers=headers)
    assert response.status_code == 400 and response.json()['code'] == CLOUD_CAMERA_REQUIRES_HLS == '403', response.text

    for partial in (False, True):
        response = await client.patch(f'/v1/cams/bulk/{camera_id}', headers=headers, json={
            'data': {'cams': [{'name': 'forbidden'}]}, 'options': {'partial_allowed': partial},
        })
        assert response.status_code == 400 and response.json()['code'] == IMPORTED_CAMERA_READ_ONLY
        response = await client.delete(f'/v1/cams/bulk/{camera_id}', headers=headers,
                                       params={'partial_allowed': str(partial).lower()})
        assert response.status_code == 400 and response.json()['code'] == IMPORTED_CAMERA_DELETE_VIA_CONNECTION

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
