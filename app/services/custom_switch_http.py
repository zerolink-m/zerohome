"""Validate custom switch destinations before saving and at connection time."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Mapping

import aiohttp
from aiohttp.resolver import ThreadedResolver
from sqlalchemy import select
from yarl import URL

from app.core.codes import FORBIDDEN_RESOURCE, SERVER_ERROR, VALIDATION_ERROR
from app.core.errors import AppError
from app.models.user import User
from app.schemas.switch import CustomRequest

_REQUEST_SECONDS = 10
_DNS_SECONDS = 5
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _check_address(host: str, allow_local: bool) -> None:
    address = ipaddress.ip_address(host.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address.sixtofour is not None:
            _check_address(str(address.sixtofour), allow_local)
        elif address.teredo is not None:
            for embedded in address.teredo:
                _check_address(str(embedded), allow_local)
        elif address in ipaddress.IPv6Network("64:ff9b::/96"):
            _check_address(str(ipaddress.IPv4Address(int(address) & 0xffffffff)), allow_local)
    if address.is_unspecified or address.is_multicast:
        raise AppError(VALIDATION_ERROR, "Адрес не обозначает HTTP-устройство")
    if not allow_local and (not address.is_global or address.is_reserved or getattr(address, "is_site_local", False)):
        raise AppError(FORBIDDEN_RESOURCE, "У владельца выключателя нет разрешения на локальные адреса")


def _target(raw_url: str, allow_local: bool) -> URL:
    try:
        url = URL(raw_url)
        host = url.raw_host
        port = url.port
    except (ValueError, UnicodeError) as exc:
        raise AppError(VALIDATION_ERROR, "Неверный адрес выключателя") from exc
    if (url.scheme not in {"http", "https"} or not host or port is None or not 0 < port <= 65535
            or url.raw_user is not None or url.raw_password is not None or url.fragment):
        raise AppError(VALIDATION_ERROR, "Неверный адрес выключателя")
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        _check_address(host, allow_local)
    return url


class _Resolver(ThreadedResolver):
    def __init__(self, allow_local: bool):
        super().__init__()
        self._allow_local = allow_local

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        candidates = await super().resolve(host, port, family)
        if not candidates:
            raise OSError("DNS returned no usable switch addresses")
        for candidate in candidates:
            _check_address(candidate["host"], self._allow_local)
        return candidates

    def make_socket(self, addr_info) -> socket.socket:
        family, kind, proto, _, address = addr_info
        # aiohttp can bypass the resolver for numeric hostnames.
        _check_address(address[0], self._allow_local)
        return socket.socket(family, kind, proto)


async def owner_allows_local(db, owner_id: int) -> bool:
    permitted = await db.scalar(select(User.local_connections_allowed).where(User.id == owner_id))
    return bool(permitted)


async def validate_metadata(db, owner_id: int, metadata: dict) -> None:
    allow_local = await owner_allows_local(db, owner_id)
    resolver = _Resolver(allow_local)
    try:
        async with asyncio.timeout(_DNS_SECONDS):
            hosts = set()
            for action in ("on", "off", "sync"):
                if (spec := metadata.get(action)) is None:
                    continue
                url = _target(spec["request"]["url"], allow_local)
                key = (url.raw_host, url.port)
                if key not in hosts:
                    await resolver.resolve(*key)
                    hosts.add(key)
    except (OSError, TimeoutError) as exc:
        raise AppError(VALIDATION_ERROR, "Не удалось определить адрес выключателя") from exc
    finally:
        await resolver.close()


@dataclass
class Reply:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


async def send(request: CustomRequest, allow_local: bool, *, read_body: bool) -> Reply:
    url = _target(request.url, allow_local)
    resolver = _Resolver(allow_local)
    connector = aiohttp.TCPConnector(
        resolver=resolver, use_dns_cache=False, socket_factory=resolver.make_socket,
    )
    headers = dict(request.headers)
    if request.body is not None:
        headers["Content-Type"] = "text/plain"
    try:
        async with aiohttp.ClientSession(
            connector=connector, trust_env=False, timeout=aiohttp.ClientTimeout(total=_REQUEST_SECONDS),
        ) as client:
            async with client.request(
                request.method, url, headers=headers, data=request.body, allow_redirects=False,
            ) as response:
                body = bytearray()
                if read_body:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise AppError(SERVER_ERROR, "Слишком большой ответ выключателя")
                        body.extend(chunk)
                return Reply(response.status, response.headers, bytes(body))
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as exc:
        raise AppError(SERVER_ERROR, "Ошибка запроса к выключателю") from exc
    finally:
        await resolver.close()
