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
The cache key is ``(access_id, endpoint)`` rather than ``connection_id``.
That way if two Connection rows happen to reference the same Tuya project
(same access_id + same region host), they share one client and one token
cache. `access_secret` is not part of the key — a secret rotation must be
followed by `invalidate(access_id, endpoint)` from the router so the next
call rebuilds the client with the new secret.

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
import logging

from app.services.tuya import TuyaCloudClient

logger = logging.getLogger(__name__)


# Module-level state. A single pool instance is fine for the process — we
# don't ship multi-tenant isolation at this layer.
_clients: dict[tuple[str, str], TuyaCloudClient] = {}
_lock: asyncio.Lock = asyncio.Lock()


def _key(access_id: str, endpoint: str) -> tuple[str, str]:
    return (access_id, endpoint)


async def get_client(
    access_id: str, access_secret: str, endpoint: str
) -> TuyaCloudClient:
    """Return a cached client for these credentials, creating one if absent.

    `access_secret` is used only when we actually build a new client — an
    existing entry is returned as-is regardless of the secret passed in.
    Callers that rotate secrets must call `invalidate()` first, otherwise
    the pool will keep serving the old client (with the old secret, which
    will then fail auth against Tuya on the next request).
    """
    key = _key(access_id, endpoint)

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


async def invalidate(access_id: str, endpoint: str) -> None:
    """Drop the cached client for these credentials and close its httpx pool.

    Called from `routers/connections.py` when the underlying Connection's
    `meta` is patched or the row is deleted — the next `get_client` call
    will rebuild with the fresh secret. Safe to call for keys that aren't
    currently cached (no-op)."""
    key = _key(access_id, endpoint)
    async with _lock:
        client = _clients.pop(key, None)
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        # Closing a stale client shouldn't be able to break the request that
        # triggered the invalidation — swallow and log.
        logger.exception(
            "tuya client close failed for access_id=%s endpoint=%s",
            access_id, endpoint,
        )
    else:
        logger.info(
            "tuya client invalidated for access_id=%s endpoint=%s", access_id, endpoint
        )


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
