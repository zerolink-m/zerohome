"""Process-wide cache of `TuyaCloudClient` instances, keyed by credentials.

Why a pool at all
-----------------
Unlike the previous module-level HTTP functions (which created a fresh
`httpx.AsyncClient` and re-fetched the access token on every call), the new
`TuyaCloudClient` is a *stateful* object:

* it owns an `httpx.AsyncClient` with a keep-alive pool (HTTP/2 when the
  `h2` package is installed),
* it caches the access token in-memory with a 2-hour TTL and refreshes it
  transparently before expiry.

Creating one per request throws that away — every toggle would still cost a
`GET /v1.0/token` round-trip. So we hold onto instances at the application
level and hand them out to callers.

Keying
------
The cache key includes the access ID, endpoint and a digest of the access
secret. Only callers with matching credentials share a token cache.

Concurrency
-----------
Client construction is guarded by a single `asyncio.Lock`. Two concurrent
toggles for the same Tuya account will not race into creating two clients
(and thus two token pools). The lock is held only around the dict mutation;
the `TuyaCloudClient` constructor is fast (no I/O) so contention is trivial.

Lifecycle
---------
`init()` is called from FastAPI startup — nothing eager to create, just
resets state to a clean pool. `close_all()` is called from shutdown and
`await`s every client's `.close()` so the httpx sessions drain gracefully.
Individual clients can be dropped mid-lifetime via `invalidate()` (called
from the router on Connection PATCH/DELETE).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from app.schemas.connection import TUYA_ENDPOINTS
from app.services.tuya import TuyaCloudClient

logger = logging.getLogger(__name__)


_clients: dict[tuple[str, str, bytes], TuyaCloudClient] = {}
_lock: asyncio.Lock = asyncio.Lock()


def _key(access_id: str, endpoint: str, access_secret: str) -> tuple[str, str, bytes]:
    return access_id, TUYA_ENDPOINTS.get(endpoint, endpoint), hashlib.sha256(access_secret.encode()).digest()


async def get_client(
    access_id: str, access_secret: str, endpoint: str
) -> TuyaCloudClient:
    """Return a cached client only for matching credentials."""
    key = _key(access_id, endpoint, access_secret)

    # Fast path: already cached, no lock needed for reads on a dict in a
    # single event loop.
    client = _clients.get(key)
    if client is not None:
        return client

    async with _lock:
        # Re-check under the lock — another coroutine may have created it
        # while we were waiting.
        client = _clients.get(key)
        if client is not None:
            return client
        client = TuyaCloudClient(
            access_id=access_id,
            access_secret=access_secret,
            endpoint=endpoint,
        )
        _clients[key] = client
        logger.info(
            "tuya client created for access_id=%s endpoint=%s (pool size=%d)",
            access_id, endpoint, len(_clients),
        )
        return client


async def invalidate(access_id: str, endpoint: str, access_secret: str) -> None:
    """Drop the cached client for these credentials and close its httpx pool.

    Called from `routers/connections.py` when the underlying Connection's
    `meta` is patched or the row is deleted — the next `get_client` call
    will rebuild with the fresh secret. Safe to call for keys that aren't
    currently cached (no-op)."""
    async with _lock:
        client = _clients.pop(_key(access_id, endpoint, access_secret), None)
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        logger.exception("tuya client close failed for access_id=%s endpoint=%s", access_id, endpoint)


async def close_all() -> None:
    """Close every cached client. Called from FastAPI shutdown.

    Runs all closes concurrently but tolerates individual failures — one
    hung httpx pool must not block the whole shutdown."""
    async with _lock:
        clients = list(_clients.values())
        _clients.clear()
    if not clients:
        return
    results = await asyncio.gather(
        *(c.close() for c in clients), return_exceptions=True
    )
    for c, r in zip(clients, results):
        if isinstance(r, Exception):
            logger.warning("tuya client close raised: %s", r)
    logger.info("tuya pool drained (%d client(s) closed)", len(clients))


def size() -> int:
    """Diagnostic: number of currently cached clients."""
    return len(_clients)
