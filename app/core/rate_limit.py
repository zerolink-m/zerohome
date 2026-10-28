from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from starlette.requests import Request

from app.core.codes import ENDPOINT_RATE_LIMITED, LOGIN_RATE_LIMITED, RATE_LIMITED
from app.core.errors import AppError
from app.core.ip import resolve_ip


# Global bucket (per-IP) applied to every request via a middleware/dep in main.py.
GLOBAL_BUCKET_NAME = "global"
GLOBAL_CAPACITY = 300           # points
GLOBAL_PER_MINUTE = 300.0       # refill points/minute

# Cleanup loop: evict idle buckets so the dict doesn't grow forever.
_CLEANUP_INTERVAL_SECONDS = 60.0
_BUCKET_IDLE_TTL_SECONDS = 600.0

_guard = asyncio.Lock()


@dataclass
class _Bucket:
    tokens: float
    capacity: float
    refill_per_second: float
    last_seen: float


_buckets: dict[str, _Bucket] = {}


async def _consume(
    key: str,
    capacity: int,
    refill_per_second: float,
    cost: float = 1,
) -> float | None:
    """Returns None if `cost` points were charged, otherwise seconds until enough points free up."""
    now = time.monotonic()
    async with _guard:
        bucket = _buckets.get(key)
        if bucket is None:
            bucket = _Bucket(
                tokens=capacity,
                capacity=capacity,
                refill_per_second=refill_per_second,
                last_seen=now,
            )
            _buckets[key] = bucket
        else:
            elapsed = now - bucket.last_seen
            bucket.tokens = min(
                bucket.capacity,
                bucket.tokens + elapsed * bucket.refill_per_second,
            )
            bucket.last_seen = now
        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return None
        return (cost - bucket.tokens) / bucket.refill_per_second


async def _peek(key: str, capacity: int, refill_per_second: float) -> float:
    """Current point balance without charging anything."""
    now = time.monotonic()
    async with _guard:
        bucket = _buckets.get(key)
        if bucket is None:
            return capacity
        elapsed = now - bucket.last_seen
        return min(
            bucket.capacity,
            bucket.tokens + elapsed * bucket.refill_per_second,
        )


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        now = time.monotonic()
        async with _guard:
            stale = [
                key
                for key, bucket in _buckets.items()
                if now - bucket.last_seen > _BUCKET_IDLE_TTL_SECONDS
            ]
            for key in stale:
                _buckets.pop(key, None)


def start_cleanup_task() -> asyncio.Task:
    return asyncio.create_task(_cleanup_loop())


# Budgets whose denial has a dedicated cursedapi code (default: 173 per endpoint).
_CODE_BY_NAME = {"auth_login": LOGIN_RATE_LIMITED}


def rate_headers(
    limit: float,
    remaining: float,
    refill_per_second: float,
    capacity: float,
    retry_after: float | None = None,
) -> dict[str, str]:
    """cursed-rate-limit-* headers (docs/headers.md). `reset` = unix time the bucket is full."""
    now = time.time()
    reset = int(now + (capacity - remaining) / refill_per_second) if refill_per_second > 0 else int(now)
    window = int(round(capacity / refill_per_second)) if refill_per_second > 0 else 0
    h = {
        "cursed-rate-limit-limit": str(int(limit)),
        "cursed-rate-limit-remaining": str(max(0, int(remaining))),
        "cursed-rate-limit-reset": str(reset),
        "cursed-rate-limit-policy": f"{int(limit)};w={window}",
    }
    if retry_after is not None:
        secs = str(max(1, int(retry_after) + 1))
        h["cursed-retry-after"] = secs
        h["Retry-After"] = secs
    return h


def _raise_rate_limited(
    retry_after: float,
    code: str = ENDPOINT_RATE_LIMITED,
    headers: dict[str, str] | None = None,
) -> None:
    h = dict(headers or {})
    secs = str(max(1, int(retry_after) + 1))
    h.setdefault("Retry-After", secs)
    h.setdefault("cursed-retry-after", secs)
    raise AppError(code, "Слишком много запросов, попробуйте позже", headers=h)


class PointBudget:
    """Per-IP points bucket shared by every caller using the same `name` — several
    endpoints can draw from one budget (e.g. opening vs. attaching to a stream) by
    passing different `cost` for the same `name`, since the bucket key is name+ip."""

    def __init__(self, name: str, capacity: int, per_minute: float, code: str | None = None):
        self.name = name
        self.code = code or _CODE_BY_NAME.get(name, ENDPOINT_RATE_LIMITED)
        self.capacity = capacity
        self.refill_per_second = per_minute / 60.0

    async def charge(self, request: Request, cost: float = 1) -> None:
        ip = resolve_ip(request)
        key = f"{self.name}:{ip}"
        retry_after = await _consume(key, self.capacity, self.refill_per_second, cost)
        if retry_after is not None:
            _raise_rate_limited(
                retry_after,
                self.code,
                rate_headers(self.capacity, 0, self.refill_per_second, self.capacity, retry_after),
            )

    async def has_at_least(self, request: Request, cost: float) -> bool:
        """Cheap pre-check for cases where the true cost is only known after doing some
        cheap classification work (e.g. is this snapshot cached?) — call this first with
        the cheapest possible cost, do the classification, then charge() the real cost."""
        ip = resolve_ip(request)
        key = f"{self.name}:{ip}"
        balance = await _peek(key, self.capacity, self.refill_per_second)
        return balance >= cost


def rate_limit(name: str, capacity: int, per_minute: float, cost: float = 1, code: str | None = None):
    """FastAPI dependency for endpoints whose point cost is fixed and known up front."""
    budget = PointBudget(name, capacity, per_minute, code)

    async def _dep(request: Request) -> None:
        await budget.charge(request, cost)

    return _dep


async def check_global(request: Request) -> dict[str, str]:
    """Charge the global per-IP bucket. Returns cursed-rate-limit-* headers to attach to the
    response; on exhaustion raises AppError(114) carrying the same headers + retry-after."""
    ip = resolve_ip(request)
    key = f"{GLOBAL_BUCKET_NAME}:{ip}"
    per_sec = GLOBAL_PER_MINUTE / 60.0
    retry_after = await _consume(key, GLOBAL_CAPACITY, per_sec)
    remaining = await _peek(key, GLOBAL_CAPACITY, per_sec)
    if retry_after is not None:
        raise AppError(
            RATE_LIMITED,
            "Слишком много запросов, попробуйте позже",
            headers=rate_headers(GLOBAL_CAPACITY, 0, per_sec, GLOBAL_CAPACITY, retry_after),
        )
    return rate_headers(GLOBAL_CAPACITY, remaining, per_sec, GLOBAL_CAPACITY)
