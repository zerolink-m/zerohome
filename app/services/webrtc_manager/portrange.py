"""Confine aioice's host UDP sockets to WEBRTC_UDP_PORT_MIN..MAX (no fork of the library).

aioice binds every ICE host socket with `loop.create_datagram_endpoint(local_addr=(ip, 0))`, i.e. the
OS picks a port from net.ipv4.ip_local_port_range (usually 32768-60999) and there is no parameter to
change that. `aioice.ice` only reaches the event loop through its own `asyncio` module reference, so
we swap that reference for a thin proxy whose loop picks a random free port inside the range for
`port 0` binds. Nothing outside `aioice.ice` is affected (TURN/STUN sockets, ingest, uvicorn...).
"""
from __future__ import annotations

import asyncio
import errno
import logging
import random

import aioice.ice

from app.core.config import settings

logger = logging.getLogger(__name__)

_ATTEMPTS = 64
_range: tuple[int, int] | None = None


class _RangedLoop:
    """Delegates to the real loop; only `create_datagram_endpoint` is overridden."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def __getattr__(self, name: str):
        return getattr(self._loop, name)

    async def create_datagram_endpoint(self, protocol_factory, local_addr=None, **kwargs):
        rng = _range
        if rng is None or not local_addr or len(local_addr) < 2 or local_addr[1] != 0:
            return await self._loop.create_datagram_endpoint(protocol_factory, local_addr=local_addr, **kwargs)
        lo, hi = rng
        last: OSError | None = None
        for _ in range(_ATTEMPTS):
            port = random.randint(lo, hi)
            try:
                return await self._loop.create_datagram_endpoint(
                    protocol_factory, local_addr=(local_addr[0], port, *local_addr[2:]), **kwargs
                )
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                last = exc
        raise last if last is not None else OSError(errno.EADDRINUSE, "no free UDP port in range")


class _AsyncioProxy:
    """Stands in for the `asyncio` name inside aioice.ice."""

    def __getattr__(self, name: str):
        return getattr(asyncio, name)

    def get_event_loop(self):
        return _RangedLoop(asyncio.get_event_loop())


_proxy = _AsyncioProxy()


def configure_port_range(lo: int | None = None, hi: int | None = None) -> bool:
    """Apply the range from settings (or explicit args). Idempotent. True if a range is active."""
    global _range
    lo = settings.WEBRTC_UDP_PORT_MIN if lo is None else lo
    hi = settings.WEBRTC_UDP_PORT_MAX if hi is None else hi
    if not (0 < lo <= hi <= 65535):
        _range = None
        return False
    if aioice.ice.asyncio is not asyncio and aioice.ice.asyncio is not _proxy:
        logger.warning("webrtc: unexpected aioice internals, UDP port range not applied")
        _range = None
        return False
    _range = (lo, hi)
    aioice.ice.asyncio = _proxy
    return True
