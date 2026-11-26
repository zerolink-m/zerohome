"""Authenticated Ucams media access with bounded, public-only upstream requests."""
import hashlib
import base64
import ipaddress
import json
import re
import socket
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

import aiohttp
from cryptography.fernet import Fernet, InvalidToken
from aiohttp.abc import AbstractResolver

from app.core.codes import BAD_GATEWAY, OBJECT_NOT_FOUND
from app.core.errors import AppError
from app.core.config import settings
from app.models.connection import Connection, ConnectionCredentials, ConnectionResource


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        import asyncio
        entries = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not entries or any(not ipaddress.ip_address(item[4][0]).is_global for item in entries):
            raise ValueError('Ucams host resolves to a non-public address')
        return [{'hostname': host, 'host': item[4][0], 'port': port, 'family': item[0],
                 'proto': item[2], 'flags': socket.AI_NUMERICHOST} for item in entries]

    async def close(self):
        pass


def checked_url(url, host):
    parts = urlsplit(url)
    if (not isinstance(host, str) or not host.endswith('.ufanet.ru')
            or parts.scheme != 'https' or parts.hostname != host or parts.username or parts.password
            or parts.port not in (None, 443) or parts.fragment):
        raise ValueError('Invalid Ucams media URL')
    return url


async def fetch(url, host, limit):
    checked_url(url, host)
    connector = aiohttp.TCPConnector(resolver=PublicResolver())
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as session:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError('Ucams media unavailable')
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError('Ucams media response too large')
            return bytes(data), response.headers.get('Content-Type', '')


async def check_camera(db, cam):
    resource = await db.get(ConnectionResource, cam.connection_resource_id)
    if resource is None or resource.resource_type != 'camera' or not resource.available or not cam.enabled:
        raise AppError(OBJECT_NOT_FOUND, 'Camera not available')
    conn = await db.get(Connection, resource.connection_id)
    creds = await db.get(ConnectionCredentials, resource.connection_id)
    if conn is None or not conn.enabled or conn.provider != 'ufanet-smart-home' or conn.uid != cam.ownuser or creds is None:
        raise AppError(OBJECT_NOT_FOUND, 'Camera not available')
    return resource, conn, creds


async def camera_details(db, cam):
    resource, conn, creds = await check_camera(db, cam)
    try:
        from ufanet_async import UfanetClient, UfanetConfig
        async with UfanetClient(UfanetConfig(contract=creds.credentials['contract'], password=creds.credentials['password'])) as client:
            await client.ucams.login()
            cameras = await client.ucams.get_camera_details([resource.external_id])
            if len(cameras) != 1 or cameras[0].number != resource.external_id:
                raise ValueError('Camera unavailable')
            return cameras[0]
    except Exception as exc:
        raise AppError(BAD_GATEWAY, 'Ucams unavailable') from exc


async def screenshot(db, cam):
    camera = await camera_details(db, cam)
    try:
        host = camera.server['screenshot_domain']
        url = f'https://{host}/api/v0/screenshots/{quote(camera.number, safe="")}~400.jpg?token={quote(camera.token_l, safe="")}'
        data, content_type = await fetch(url, host, 8 * 1024 * 1024)
        if not content_type.lower().startswith('image/jpeg') or not data.startswith(b'\xff\xd8'):
            raise ValueError('Invalid JPEG')
        return data
    except Exception as exc:
        raise AppError(BAD_GATEWAY, 'Ucams snapshot unavailable') from exc


async def playlist(db, cam, start=None, duration=None):
    camera = await camera_details(db, cam)
    try:
        from ufanet_async.services import UcamsClient
        url = (UcamsClient.live_hls_url(camera) if start is None else
               UcamsClient.archive_hls_url(camera, start, duration))
        host = camera.server['domain']
        data, _ = await fetch(url, host, 1024 * 1024)
        text = data.decode('utf-8-sig')
        if not text.startswith('#EXTM3U'):
            raise ValueError('Invalid playlist')
        return text, url, host
    except Exception as exc:
        raise AppError(BAD_GATEWAY, 'Ucams playlist unavailable') from exc


_URI_ATTRIBUTE = re.compile(r'(?<![\w-])URI="([^"\r\n]+)"', re.IGNORECASE)


def resource_key(url):
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() != 'token'])
    return hashlib.sha256((parts.path + '?' + query).encode()).hexdigest()


def _cipher():
    secret = settings.JWT_SECRET_SHARE
    if not secret:
        raise ValueError('Missing shared media signing secret')
    key = hashlib.sha256(('ufanet-hls-v1:' + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal_resource(cam_id, url, host, scope):
    checked_url(url, host)
    payload = json.dumps([cam_id, url, host, scope], separators=(',', ':')).encode()
    return _cipher().encrypt(payload).decode()


def open_resource(token, cam_id, scope):
    if len(token) > 8192:
        raise ValueError('Invalid resource')
    try:
        payload = json.loads(_cipher().decrypt(token.encode(), ttl=3600))
        saved_id, url, host, saved_scope = payload
        if saved_id != cam_id or saved_scope != scope:
            raise ValueError('Invalid resource')
        return checked_url(url, host), host
    except (InvalidToken, ValueError, TypeError, UnicodeError) as exc:
        raise ValueError('Invalid resource') from exc


def entries(text, base, host):
    result = {}
    for line in text.splitlines():
        entry = line.strip()
        uris = _URI_ATTRIBUTE.findall(entry) if entry.startswith('#') else ([entry] if entry else [])
        if entry.startswith('#') and re.search(r'(?<![\w-])URI\s*=', entry, re.IGNORECASE) and not uris:
            raise ValueError('Invalid HLS URI attribute')
        for uri in uris:
            url = checked_url(urljoin(base, uri), host)
            if urlsplit(url).path.lower().endswith('.m3u8'):
                raise ValueError('Nested playlists are not supported')
            key = resource_key(url)
            if key in result and result[key] != url:
                raise ValueError('Ambiguous HLS resource')
            result[key] = url
    return result


def rewrite(text, base, host, resource_url, *, protected_cam=None, scope='live'):
    mapping = entries(text, base, host)
    def replace(uri):
        url = checked_url(urljoin(base, uri), host)
        key = resource_key(url)
        if key not in mapping:
            raise ValueError('Unknown HLS resource')
        value = seal_resource(protected_cam, url, host, scope) if protected_cam is not None else key
        return resource_url(value)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            line = _URI_ATTRIBUTE.sub(lambda match: 'URI="' + replace(match.group(1)) + '"', line)
        elif stripped:
            line = replace(stripped)
        lines.append(line)
    return '\n'.join(lines) + '\n'
