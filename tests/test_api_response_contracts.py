"""Coverage and representative wire-level checks for generated API contracts."""
import ast
import inspect
import re
import textwrap

import pytest

from app.core import codes
from app.core.introspection import _flatten
from app.routers.cams import router as cams_router
from app.schemas.user import UserOut
from tests.conftest import bearer, mint_test_token

pytestmark = pytest.mark.asyncio


async def test_explicit_handler_errors_are_in_contracts(app):
    for route in _flatten(app.router.routes):
        handler = getattr(route, 'endpoint', None)
        contract = getattr(handler, 'zerohome_contract', None)
        if contract is None:
            continue
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
        documented = {getattr(spec.code, 'code', spec.code)
                      for spec in contract.arguments.get('responses', {}).values()}
        # The library always provides these generic response variants.
        documented.update((codes.SERVER_ERROR, codes.FORBIDDEN, codes.VALIDATION_ERROR))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'AppError' and node.args):
                continue
            code_node = node.args[0]
            if isinstance(code_node, ast.Name):
                value = handler.__globals__.get(code_node.id)
            elif isinstance(code_node, ast.Attribute) and isinstance(code_node.value, ast.Name) and code_node.value.id == 'codes':
                value = getattr(codes, code_node.attr, None)
            else:
                continue  # Dynamic codes are covered by route-specific integration tests.
            if isinstance(value, str) and value.isdecimal():
                assert value in documented, (route.path, handler.__name__, value)


async def test_all_public_http_operations_and_websockets_have_contracts(client, app):
    document = (await client.get('/v1/openapi.json')).json()
    methods = {'GET', 'POST', 'PATCH', 'PUT', 'DELETE'}
    expected = []
    for route in _flatten(app.router.routes):
        path = getattr(route, 'path', '')
        if not path.startswith('/v1/') or path in {'/v1/docs', '/v1/openapi.json', '/v1/cursedapi.md'} or '/_cursed/' in path:
            continue
        for method in (getattr(route, 'methods', None) or set()) & methods:
            expected.append((path, method))
            operation = document['paths'][path][method.lower()]
            assert operation.get('summary'), (path, method)
            variants = [variant for response in operation['responses'].values()
                        for variant in response.get('x-cursedapi-variants', [])]
            assert variants and all(variant.get('description') for variant in variants), (path, method)
            assert all(re.search('[А-Яа-яЁё]', variant['description']) for variant in variants), (path, method)
            assert any(codes.http_status(variant['code']) < 400 for variant in variants), (path, method)
            assert any(int(status) >= 400 for status in operation['responses']), (path, method)
    assert expected
    websocket_paths = {r.path for r in cams_router.routes if 'websocket' in type(r).__name__.lower()}
    assert websocket_paths == set(document['x-websocket']) == {'/v1/cams/{id}/webrtc'}
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get('$ref', '')
            if ref.startswith('#/components/schemas/'):
                assert ref.rsplit('/', 1)[-1] in document['components']['schemas'], ref
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    signaling = document['x-websocket']['/v1/cams/{id}/webrtc']
    assert {name: msg['direction'] for name, msg in signaling['messages'].items()} == {
        'offer': 'server-to-client', 'answer': 'client-to-server',
        'ice_candidate': 'client-to-server', 'error': 'server-to-client',
    }
    markdown = (await client.get('/v1/cursedapi.md')).text
    for path, method in expected:
        assert f'`{method} {path}`' in markdown
    assert '`WS /v1/cams/{id}/webrtc`' in markdown


async def test_discovery_parameters_auth_and_actual_json(client, mk_user):
    uid = await mk_user('contracts@example.org')
    headers = bearer(await mint_test_token(uid))
    path = '/v1/cams/42/hls/archive/playlist.m3u8'
    anonymous = await client.options(path)
    assert 'cursed-schema-url' not in anonymous.headers
    live_options = await client.options('/v1/cams/42/hls/playlist.m3u8')
    assert live_options.headers['cursed-auth'] == 'bearer,token'
    assert 'cursed-schema-url' not in live_options.headers
    invalid = await client.options(path, headers=bearer('invalid-token'))
    assert invalid.status_code == 200 and 'cursed-schema-url' not in invalid.headers
    public = await client.options('/v1/auth/login', headers=bearer('invalid-token'))
    assert public.status_code == 200 and 'POST' in public.headers['allow']
    assert (await client.get('/v1/_cursed/schema' + path)).status_code == 401
    options = await client.options(path, headers=headers)
    schema = (await client.get(options.headers['cursed-schema-url'], headers=headers)).json()['data']['schema']
    get = schema['methods']['GET']
    assert schema['path_parameters']['id']['type'] == 'integer'
    assert get['query']['start']['required'] and get['query']['duration']['required']
    assert get['query']['duration']['schema']['maximum'] == 86400
    assert any(v['content_type'] == 'application/vnd.apple.mpegurl' for v in get['responses']['200'])
    missing_query = await client.get(path, headers=headers)
    assert missing_query.json()['code'] == codes.QUERY_REQUIRED
    assert any(v['code'] == codes.QUERY_REQUIRED for v in get['responses'][str(missing_query.status_code)])

    recordings = await client.options('/v1/cams/42/recordings', headers=headers)
    shape = (await client.get(recordings.headers['cursed-schema-url'], headers=headers)).json()['data']['schema']['methods']['GET']
    assert {'from', 'to', 'limit', 'offset'} <= set(shape['query'])
    snapshot = await client.options('/v1/cams/42/recordings/a.mp4/snapshot', headers=headers)
    shape = (await client.get(snapshot.headers['cursed-schema-url'], headers=headers)).json()['data']['schema']['methods']['GET']
    assert {'id', 'recording_id'} <= set(shape['path'])
    assert 'at' in shape['query']
    assert any(v['content_type'] == 'image/jpeg' for v in shape['responses']['200'])
    bulk_options = await client.options('/v1/cams/bulk/42', headers=headers)
    bulk_schema = (await client.get(bulk_options.headers['cursed-schema-url'], headers=headers)).json()['data']['schema']['methods']['DELETE']
    assert not bulk_schema['request']['required']
    assert not any(v['code'] == codes.EMPTY_BODY for variants in bulk_schema['responses'].values() for v in variants)
    motion_options = await client.options('/v1/cams/42/motion/events', headers=headers)
    motion_shape = (await client.get(motion_options.headers['cursed-schema-url'], headers=headers)).json()['data']['schema']['methods']['GET']
    assert motion_shape['query']['limit']['schema']['default'] == 50
    assert motion_shape['query']['limit']['schema']['maximum'] == 200
    assert motion_shape['query']['order']['schema']['default'] == 'desc'

    unsupported = await client.get('/v1/users', headers=headers, params={'filter': 'id==1'})
    assert unsupported.json()['code'] == codes.FUNCTION_NOT_SUPPORTED
    users_options = await client.options('/v1/users', headers=headers)
    users_shape = (await client.get(users_options.headers['cursed-schema-url'], headers=headers)).json()['data']['schema']['methods']['GET']
    assert any(v['code'] == codes.FUNCTION_NOT_SUPPORTED for v in users_shape['responses'][str(unsupported.status_code)])

    real = await client.get('/v1/users/me', headers=headers)
    assert real.status_code == 200 and real.json()['code'] == codes.SUCCESS
    UserOut.model_validate(real.json()['data']['user'])
    user_options = await client.options('/v1/users/me', headers=headers)
    user_shape = (await client.get(user_options.headers['cursed-schema-url'], headers=headers)).json()['data']['schema']['methods']['GET']
    variant = next(v for v in user_shape['responses']['200'] if v['code'] == real.json()['code'])
    assert variant['body_schema']['properties']['data']['properties']['user']
    bad = await client.post('/v1/auth/login', json={'data': {'credentials': {'email': 'nobody@example.org', 'password': 'not-a-password'}}})
    assert bad.status_code == 400 and bad.json()['code'] == codes.BAD_CREDENTIALS
    document = (await client.get('/v1/openapi.json')).json()
    assert not document['paths']['/v1/auth/login']['post'].get('security')
    assert any(v['code'] == bad.json()['code'] for v in document['paths']['/v1/auth/login']['post']['responses']['400']['x-cursedapi-variants'])
    login = document['paths']['/v1/auth/login']['post']
    assert '403' not in login['responses']
    assert '403' not in document['paths']['/v1/health']['get']['responses']
    assert {v['code'] for v in document['paths']['/v1/config']['get']['responses']['403']['x-cursedapi-variants']} == {codes.FRONTEND_UPDATE_NEEDED}
    assert login['requestBody']['content']['application/json']['example']['data']['credentials']['email'] == 'anna@example.org'
    example = login['responses']['200']['content']['application/json']['examples']['result']['value']
    assert example['data']['token'] == '<session-token>'
    assert {'request_id', 'generated_at', 'cursed_version'} <= set(example['metadata'])
    bad_refresh = await client.post('/v1/auth/new-refresh', headers=bearer('not-a-jwt'))
    assert bad_refresh.json()['code'] == codes.TOKEN_FORMAT_INVALID
    refresh = document['paths']['/v1/auth/new-refresh']['post']['responses']
    assert any(v['code'] == codes.TOKEN_FORMAT_INVALID for response in refresh.values() for v in response.get('x-cursedapi-variants', []))


async def test_global_rate_limit_matches_public_and_raw_contracts(client, monkeypatch):
    from app.core import middleware
    from app.core.errors import AppError

    document = (await client.get('/v1/openapi.json')).json()
    async def reject(_request):
        raise AppError(codes.RATE_LIMITED, 'Тестовый общий лимит')

    monkeypatch.setattr(middleware, 'check_global', reject)
    for method, path, template in (
        ('GET', '/v1/health', '/v1/health'),
        ('GET', '/v1/version', '/v1/version'),
        ('GET', '/v1/config/frontend', '/v1/config/frontend'),
        ('POST', '/v1/auth/login', '/v1/auth/login'),
        ('GET', '/v1/cams/1/stream', '/v1/cams/{id}/stream'),
    ):
        response = await client.request(method, path)
        assert response.status_code == 429 and response.json()['code'] == codes.RATE_LIMITED
        assert response.headers['content-type'].startswith('application/json')
        variants = document['paths'][template][method.lower()]['responses']['429']['x-cursedapi-variants']
        assert any(variant['code'] == response.json()['code'] for variant in variants)


async def test_raw_types_range_and_streaming_code_are_described(client):
    document = (await client.get('/v1/openapi.json')).json()
    paths = document['paths']
    stream = paths['/v1/cams/{id}/stream']['get']
    assert 'video/mp4' in stream['responses']['200']['content']
    assert 'application/json' not in stream['responses']['200']['content']
    assert 'application/json' in stream['responses']['404']['content']
    file = paths['/v1/cams/{id}/recordings/{recording_id}/file']['get']
    assert {'video/mp4', 'multipart/byteranges'} <= set(file['responses']['206']['content'])
    assert 'text/plain' in file['responses']['416']['content']
    assert {'Range', 'If-Range'} <= {p['name'] for p in file['parameters'] if p['in'] == 'header'}
    assert '304' not in file['responses']
    hls = paths['/v1/cams/{id}/hls/{segment}']['get']
    assert {'video/mp2t', '*/*'} <= set(hls['responses']['200']['content'])
    assert set(hls['responses']['206']['content']) == {'video/mp2t', 'multipart/byteranges'}
    assert {v['code'] for v in hls['responses']['206']['x-cursedapi-variants']} == {codes.STREAMING}
    assert hls['responses']['206']['headers']['cursed-code']['schema']['enum'] == [codes.STREAMING]
    assert hls['security'] == [{'ZeroHomeBearer': []}, {'ZeroHomeQueryToken': []}]


async def test_switch_request_documents_protocol_metadata_and_non_nullable_patch(client):
    from pydantic import ValidationError
    from app.schemas.cams import CamPatch
    from app.schemas.switch import SwitchCreate

    document = (await client.get('/v1/openapi.json')).json()
    schema = document['paths']['/v1/switches']['post']['requestBody']['content']['application/json']['schema']
    switch_ref = schema['properties']['data']['properties']['switch']['$ref']
    create = document['components']['schemas'][switch_ref.rsplit('/', 1)[-1]]
    assert 'control_metadata' in create['required']
    assert len(create['properties']['control_metadata']['oneOf']) == 3
    local = next(condition for condition in create['allOf'] if condition['if']['properties']['control_protocol']['const'] == 'tuya_local')
    assert set(local['then']['properties']['control_metadata']['required']) == {'device_id', 'ip', 'local_key', 'version'}
    with pytest.raises(ValidationError):
        SwitchCreate.model_validate({'control_protocol': 'tuya_local', 'control_metadata': {}})
    with pytest.raises(ValidationError):
        CamPatch.model_validate({'name': None})
    assert CamPatch.model_validate({}).model_fields_set == set()
    patch_schema = document['paths']['/v1/cams/{id}']['patch']['requestBody']['content']['application/json']['schema']
    cam_ref = patch_schema['properties']['data']['properties']['cam']['$ref']
    patch = document['components']['schemas'][cam_ref.rsplit('/', 1)[-1]]
    assert patch['properties']['name']['type'] == 'string'
    assert 'name' not in patch.get('required', [])


async def test_common_errors_and_union_explanations_are_in_all_documents(client, mk_user):
    uid = await mk_user('descriptions@example.org')
    headers = bearer(await mint_test_token(uid))
    document = (await client.get('/v1/openapi.json')).json()
    seen = set()
    for operation_group in document['paths'].values():
        for operation in operation_group.values():
            for response in operation.get('responses', {}).values():
                for variant in response.get('x-cursedapi-variants', []):
                    if variant['content_type'] != 'application/json':
                        continue
                    if variant['code'] == codes.FORBIDDEN:
                        assert 'Доступ к действию запрещён' in variant['description']
                        seen.add(codes.FORBIDDEN)
                    if variant['code'] == codes.SERVER_ERROR:
                        assert 'Сервер не смог выполнить запрос' in variant['description']
                        seen.add(codes.SERVER_ERROR)
    assert seen == {codes.FORBIDDEN, codes.SERVER_ERROR}
    options = await client.options('/v1/cams/1', headers=headers)
    schema = (await client.get(options.headers['cursed-schema-url'], headers=headers)).json()['data']['schema']['methods']['GET']
    assert any('Доступ к действию запрещён' in variant['description'] for variant in schema['responses']['403'])
    assert any('Сервер не смог выполнить запрос' in variant['description'] for variant in schema['responses']['500'])
    markdown = (await client.get('/v1/cursedapi.md')).text
    for phrase in ('Учётные данные не включаются в ограниченный ответ',
                   'Приглашённый получает ограниченную запись без notify',
                   'tuya_local — обязательны device_id, ip, local_key, version',
                   'Каждое действие custom содержит request',
                   'для tuya_cloud обязательны access_id, access_secret и endpoint',
                   'при полном отказе', 'Сервер не смог выполнить запрос',
                   'Доступ к действию запрещён'):
        assert phrase.casefold() in markdown.casefold()


async def test_documented_owner_invitee_and_bulk_results_match_wire(client, mk_user, mk_cam, db, monkeypatch):
    from app.models.invitation import Invitation
    from app.models.switch import Switch
    from app.schemas.cams import CamOutFull, CamOutRestricted
    from app.schemas.switch import SwitchOutFull, SwitchOutRestricted
    from app.services import snapshot

    owner = await mk_user('contract-owner@example.org')
    guest = await mk_user('contract-guest@example.org')
    cam_id = await mk_cam(owner)
    async with db() as session:
        switch = Switch(ownuser=owner, name='switch', enabled=1, control_proto='custom',
                        controlmeta={'example': 'not-a-secret'}, state=0, state_updated_at=1,
                        added_at=1, updated_at=1)
        session.add(switch)
        await session.flush()
        switch_id = switch.id
        for device_type, device_id in [('cams', cam_id), ('switches', switch_id)]:
            session.add(Invitation(device_type=device_type, device_id=device_id,
                                   email='contract-guest@example.org', invited_by=owner,
                                   status='accepted', added_at=1))
        await session.commit()
    owner_headers = bearer(await mint_test_token(owner))
    guest_headers = bearer(await mint_test_token(guest))
    for path, key, full, limited, private in (
        (f'/v1/cams/{cam_id}', 'cam', CamOutFull, CamOutRestricted, 'password'),
        (f'/v1/switches/{switch_id}', 'switch', SwitchOutFull, SwitchOutRestricted, 'control_metadata'),
    ):
        full_data = (await client.get(path, headers=owner_headers)).json()['data'][key]
        limited_data = (await client.get(path, headers=guest_headers)).json()['data'][key]
        full.model_validate(full_data)
        limited.model_validate(limited_data)
        assert private in full_data and private not in limited_data

    good = {'protocol': 'rtsp', 'address': 'example.invalid', 'port': 554, 'username': 'u', 'password': 'p'}
    result = await client.post('/v1/cams/bulk', headers=owner_headers, json={
        'data': {'cams': [good, {'protocol': 'rtsp'}]}, 'options': {'partial_allowed': True},
    })
    assert result.json()['code'] == codes.PARTIALLY_CREATED
    assert len(result.json()['data']['cams']['succeeded']) == 1
    assert result.json()['data']['cams']['failed'][0]['id'] == 1
    document = (await client.get('/v1/openapi.json')).json()
    partials = document['paths']['/v1/cams/bulk']['post']['responses'][str(result.status_code)]['x-cursedapi-variants']
    assert any(v['code'] == codes.PARTIALLY_CREATED and 'позицию во входном массиве' in v['description'] for v in partials)

    async def snapshot_failure(_cam):
        raise snapshot.SnapshotError('example failure')

    monkeypatch.setattr(snapshot, 'get_live_snapshot', snapshot_failure)
    failed = await client.get(f'/v1/cams/{cam_id}/snapshot', headers=owner_headers)
    assert failed.status_code == 500 and failed.json()['code'] == codes.SERVER_ERROR
    response = document['paths']['/v1/cams/{id}/snapshot']['get']['responses']['500']
    assert any(v['code'] == codes.SERVER_ERROR and 'Сервер не смог выполнить запрос' in v['description'] for v in response['x-cursedapi-variants'])


async def test_share_create_and_read_describe_different_secret_fields(client, mk_user, mk_cam, monkeypatch):
    from app.core.config import settings
    from app.schemas.sharing import SharingCreateOut, SharingOut

    monkeypatch.setattr(settings, 'JWT_SECRET_SHARE', 'example-test-only-signing-secret-with-enough-entropy')
    uid = await mk_user('share-contract@example.org')
    cam_id = await mk_cam(uid)
    headers = bearer(await mint_test_token(uid))
    created = await client.post('/v1/shares', headers=headers, json={'data': {'share': {
        'device_type': 'cams', 'device_id': cam_id, 'type': 'stream',
    }}})
    assert created.status_code == 201
    value = SharingCreateOut.model_validate(created.json()['data']['share'])
    assert value.token
    read = await client.get(f'/v1/shares/{value.id}', headers=headers)
    assert read.status_code == 200
    SharingOut.model_validate(read.json()['data']['share'])
    assert 'token' not in read.json()['data']['share']
    doc = (await client.get('/v1/openapi.json')).json()['paths']
    create_schema = doc['/v1/shares']['post']['responses']['201']['x-cursedapi-variants'][0]['schema']
    read_schema = doc['/v1/shares/{id}']['get']['responses']['200']['x-cursedapi-variants'][0]['schema']
    assert 'token' in create_schema['properties']['data']['properties']['share']['properties']
    assert 'token' not in read_schema['properties']['data']['properties']['share']['properties']


async def test_local_hls_range_wire_status_and_code_match_contract(client, mk_user, mk_cam, monkeypatch, tmp_path):
    from app.core.config import settings
    from app.routers.cams import hls

    owner = await mk_user('hls-contract@example.org')
    cam_id = await mk_cam(owner)
    headers = bearer(await mint_test_token(owner))
    (tmp_path / 'segment.ts').write_bytes(b'0123456789')

    class FakeSession:
        temp_dir = tmp_path

        def touch(self):
            pass

    async def get_session(cam):
        return FakeSession()

    monkeypatch.setattr(settings, 'HLS_ENABLED', True)
    monkeypatch.setattr(hls, 'worker_enabled', lambda *_: True)
    monkeypatch.setattr(hls.hls_manager, 'get_or_create_session', get_session)
    monkeypatch.setattr(hls.hls_manager, 'schedule_idle_stop', lambda *_: None)
    path = f'/v1/cams/{cam_id}/hls/segment.ts'
    partial = await client.get(path, headers={**headers, 'Range': 'bytes=0-2'})
    assert partial.status_code == 206 and partial.content == b'012'
    assert partial.headers['content-type'] == 'video/mp2t'
    assert partial.headers['cursed-code'] == codes.STREAMING
    invalid = await client.get(path, headers={**headers, 'Range': 'bytes=50-60'})
    assert invalid.status_code == 416 and invalid.headers['cursed-code'] == codes.STREAMING
    etag = (await client.get(path, headers=headers)).headers['etag']
    conditional = await client.get(path, headers={**headers, 'If-None-Match': etag})
    assert conditional.status_code == 200
