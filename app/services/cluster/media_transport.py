"""The only place cluster media opens a socket.

Cluster media intentionally uses plain TCP on the trusted network. When TLS is added,
wrap the socket here and nowhere else (server accept and client dial). Until
that exists the fMP4 payload is readable by anyone on the path.

CLUSTER_SECRET only authenticates the handshake (see media_protocol). An empty
secret refuses every non-loopback peer and every non-loopback dial, so a node
cannot publish camera media onto a network by accident. A non-empty secret does
not encrypt. Do not point CLUSTER_MEDIA_HOST at a non-loopback address, and do
not run this between hosts, across a network you do not trust, until TLS wraps
the socket below.
"""
from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable


def _tcp_nodelay(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


async def open_connection(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Dial `host:port`. TLS, when it exists, wraps this connection here."""
    reader, writer = await asyncio.open_connection(host, port)
    _tcp_nodelay(writer)
    return reader, writer


async def start_server(
    host: str,
    port: int,
    client_connected: Callable[[asyncio.StreamReader, asyncio.StreamWriter], asyncio.Future | object],
) -> asyncio.Server:
    """Listen on `host:port`. TLS, when it exists, wraps each accepted socket here."""

    async def _accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _tcp_nodelay(writer)
        await client_connected(reader, writer)

    return await asyncio.start_server(_accepted, host, port)
