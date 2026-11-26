import asyncio
import pytest
from sqlalchemy import select

from app.models.connection import Connection, ConnectionCredentials, ConnectionResource
from app.services.connection_sync.ufanet_skuds import materialize_ufanet_skuds
from tests.conftest import bearer, mint_test_token


@pytest.mark.asyncio
async def test_materialized_skud_open(client, mk_user, sessionmaker_, monkeypatch):
    owner = await mk_user('skud-owner@example.org')
    other = await mk_user('skud-other@example.org')
    async with sessionmaker_() as db:
        connection = Connection(uid=owner, provider='ufanet-smart-home', name='Home', meta={}, enabled=1, added_at=1, updated_at=1)
        db.add(connection)
        await db.flush()
        db.add(ConnectionCredentials(connection_id=connection.id, credentials={'contract': '001', 'password': 'secret'}, updated_at=1))
        db.add(ConnectionResource(connection_id=connection.id, resource_type='skud', external_id='00042', name='Door', capabilities={'open': True}, attributes={}, available=1, first_seen_at=1, last_seen_at=1))
        await db.flush()
        switch = (await materialize_ufanet_skuds(db, connection.id))[0]
        assert switch.enabled == 0
        switch.enabled = 1
        switch_id = switch.id
        connection_id = connection.id
        await db.commit()

    calls = []
    async def fake_open(contract, password, external_id):
        calls.append((contract, password, external_id))
        return True
    monkeypatch.setattr('app.services.ufanet_open.open_shared', fake_open)
    headers = bearer(await mint_test_token(owner))
    response = await client.get(f'/v1/switches/{switch_id}/state', headers=headers)
    assert response.status_code != 200
    response = await client.post(f'/v1/switches/{switch_id}/state', headers=headers, json={'data': {'state': {'state': 1}}})
    assert response.status_code != 200
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_open(*args):
        started.set()
        await finish.wait()
        return True

    monkeypatch.setattr('app.services.ufanet_open.open_shared', slow_open)
    first = asyncio.create_task(client.post(f'/v1/switches/{switch_id}/open', headers=headers))
    await started.wait()
    concurrent = await client.post(f'/v1/switches/{switch_id}/open', headers=headers)
    assert concurrent.status_code != 200
    finish.set()
    assert (await first).status_code == 200
    monkeypatch.setattr('app.services.ufanet_open.open_shared', fake_open)
    response = await client.post(f'/v1/switches/{switch_id}/open', headers=headers)
    assert response.status_code == 200, response.text
    assert calls == [('001', 'secret', '00042')]
    response = await client.post(f'/v1/switches/{switch_id}/open', headers=bearer(await mint_test_token(other)))
    assert response.status_code in (403, 404)
    assert len(calls) == 1

    # Accepted invitations and device-use links authorize the same operation;
    # pending, revoked, and unrelated access must not send a command.
    other_headers = bearer(await mint_test_token(other))
    invitation = await client.post('/v1/invitations', headers=headers, json={'data': {'invitation': {
        'device_type': 'switches', 'device_id': switch_id, 'email': 'skud-other@example.org',
    }}})
    assert invitation.status_code == 201, invitation.text
    invitation_id = invitation.json()['data']['invitation']['id']
    assert (await client.post(f'/v1/switches/{switch_id}/open', headers=other_headers)).status_code == 404
    assert (await client.post(f'/v1/invitations/{invitation_id}/accept', headers=other_headers)).status_code == 200
    assert (await client.post(f'/v1/switches/{switch_id}/open', headers=other_headers)).status_code == 200
    share = await client.post('/v1/shares', headers=headers, json={'data': {'share': {
        'device_type': 'switches', 'device_id': switch_id, 'type': 'device_use',
    }}})
    assert share.status_code == 201, share.text
    share_data = share.json()['data']['share']
    share_headers = bearer(share_data['token'])
    assert (await client.post(f'/v1/switches/{switch_id}/open', headers=share_headers)).status_code == 200
    assert (await client.delete(f"/v1/shares/{share_data['id']}", headers=headers)).status_code == 200
    assert (await client.post(f'/v1/switches/{switch_id}/open', headers=share_headers)).status_code != 200
    assert len(calls) == 3

    async def denied(*args):
        return False
    monkeypatch.setattr('app.services.ufanet_open.open_shared', denied)
    response = await client.post(f'/v1/switches/{switch_id}/open', headers=headers)
    assert response.status_code == 502

    # Provider access changes must not overwrite the user's local choice.
    async with sessionmaker_() as db:
        connection = await db.get(Connection, connection_id)
        connection.enabled = 0
        await db.commit()
    assert (await client.get(f'/v1/switches/{switch_id}', headers=headers)).status_code == 404
    async with sessionmaker_() as db:
        connection = await db.get(Connection, connection_id)
        connection.enabled = 1
        resource = (await db.execute(select(ConnectionResource).where(ConnectionResource.connection_id == connection_id))).scalar_one()
        resource.capabilities = {'open': False}
        await db.commit()
    assert (await client.get(f'/v1/switches/{switch_id}', headers=headers)).status_code == 404
    async with sessionmaker_() as db:
        resource = (await db.execute(select(ConnectionResource).where(ConnectionResource.connection_id == connection_id))).scalar_one()
        resource.capabilities = {'open': True}
        await db.commit()
    restored = await client.get(f'/v1/switches/{switch_id}', headers=headers)
    assert restored.status_code == 200
    assert restored.json()['data']['switch']['enabled'] == 1
