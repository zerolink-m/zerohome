from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from app.core.config import settings
from app.models.connection import Connection, ConnectionCredentials, ConnectionResource
from app.services.connection_sync.ufanet_cameras import materialize_ufanet_cameras
from tests.conftest import mint_test_token


@pytest.mark.asyncio
@pytest.mark.parametrize('archive', [False, True])
async def test_cloud_hls_playlist_and_resource(client, mk_user, sessionmaker_, monkeypatch, archive):
    import ufanet_async
    from app.services import ufanet_media

    owner = await mk_user('hls-owner@example.org')
    other = await mk_user('hls-other@example.org')
    async with sessionmaker_() as db:
        connection = Connection(uid=owner, provider='ufanet-smart-home', name='Home', meta={}, enabled=1, added_at=1, updated_at=1)
        db.add(connection)
        await db.flush()
        db.add(ConnectionCredentials(connection_id=connection.id, credentials={'contract': '001', 'password': 'secret'}, updated_at=1))
        db.add(ConnectionResource(connection_id=connection.id, resource_type='camera', external_id='001', name='Camera', capabilities={}, attributes={}, available=1, first_seen_at=1, last_seen_at=1))
        await db.flush()
        cam = (await materialize_ufanet_cameras(db, connection.id))[0]
        cam.enabled = 1
        cam_id = cam.id
        connection_id = connection.id
        await db.commit()

    class FakeClient:
        def __init__(self, config):
            self.ucams = self
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def login(self):
            return True
        async def get_camera_details(self, numbers):
            assert numbers == ['001']
            return [SimpleNamespace(number='001', server={'domain': 'cam.ufanet.ru'}, token_l='live-secret', token_r='archive-secret')]

    calls = []
    async def fake_fetch(url, host, limit):
        ufanet_media.checked_url(url, host)
        calls.append(url)
        if urlsplit(url).path.endswith('.m3u8'):
            assert ('archive-secret' if archive else 'live-secret') in url
            return b'#EXTM3U\n#EXTINF:2,\nseg.ts?token=upstream-secret\n', 'application/vnd.apple.mpegurl'
        assert url.endswith('seg.ts?token=upstream-secret')
        return b'segment', 'video/mp2t'

    monkeypatch.setattr(ufanet_async, 'UfanetClient', FakeClient)
    monkeypatch.setattr(ufanet_media, 'fetch', fake_fetch)
    monkeypatch.setattr(settings, 'HLS_ENABLED', True)
    token = await mint_test_token(owner)
    path = f'/v1/cams/{cam_id}/hls/' + ('archive/playlist.m3u8?start=100&duration=60&' if archive else 'playlist.m3u8?') + f'token={token}'
    response = await client.get(path)
    assert response.status_code == 200, response.text
    assert 'secret' not in response.text
    segment = next(line for line in response.text.splitlines() if not line.startswith('#'))
    resource = await client.get(str(response.url.join(segment)))
    assert resource.status_code == 200, resource.text
    assert resource.content == b'segment'
    assert 'no-store' in resource.headers['cache-control'] or 'no-cache' in resource.headers['cache-control']
    denied = await client.get(path.replace(token, await mint_test_token(other)))
    assert denied.status_code in (403, 404)
    assert len(calls) == 2
    async with sessionmaker_() as db:
        connection = await db.get(Connection, connection_id)
        connection.enabled = 0
        await db.commit()
    revoked = await client.get(str(response.url.join(segment)))
    assert revoked.status_code == 404
    assert len(calls) == 2


def test_segment_descriptor_survives_playlist_window_and_is_bound(monkeypatch):
    from app.services import ufanet_media

    monkeypatch.setattr(settings, 'JWT_SECRET_SHARE', 'shared-test-secret')
    old = 'https://cam.ufanet.ru/old.ts?token=private'
    token = ufanet_media.seal_resource(12, old, 'cam.ufanet.ru', 'live')
    assert 'private' not in token
    assert ufanet_media.open_resource(token, 12, 'live') == (old, 'cam.ufanet.ru')
    with pytest.raises(ValueError):
        ufanet_media.open_resource(token, 13, 'live')
    with pytest.raises(ValueError):
        ufanet_media.open_resource(token, 12, 'archive:1:60')


def test_hls_rejects_external_resources():
    from app.services.ufanet_media import entries
    with pytest.raises(ValueError):
        entries('#EXTM3U\nhttps://evil.example/segment.ts\n', 'https://cam.ufanet.ru/001/index.m3u8', 'cam.ufanet.ru')
    with pytest.raises(ValueError):
        entries('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://evil.example/key"\n', 'https://cam.ufanet.ru/001/index.m3u8', 'cam.ufanet.ru')
