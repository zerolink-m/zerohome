import asyncio

import pytest
from starlette.requests import Request

from app.core import rate_limit as rl
from app.core.codes import ENDPOINT_RATE_LIMITED, RATE_LIMITED
from app.core.errors import AppError


def make_request(ip="203.0.113.1", headers=None):
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (ip, 1234),
    }
    return Request(scope)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(rl.time, "monotonic", c)
    return c


async def test_consume_until_empty_then_denied(clock):
    for _ in range(3):
        assert await rl._consume("k", 3, 1.0) is None
    wait = await rl._consume("k", 3, 1.0)
    assert wait == pytest.approx(1.0)


async def test_refill_over_time(clock):
    for _ in range(3):
        await rl._consume("k", 3, 1.0)
    assert await rl._consume("k", 3, 1.0) is not None
    clock.t += 1.0
    assert await rl._consume("k", 3, 1.0) is None
    assert await rl._consume("k", 3, 1.0) is not None


async def test_refill_capped_at_capacity(clock):
    await rl._consume("k", 3, 1.0)
    clock.t += 10_000
    assert await rl._peek("k", 3, 1.0) == 3


async def test_cost_greater_than_one_and_retry_after(clock):
    assert await rl._consume("k", 10, 1.0, cost=10) is None
    assert await rl._consume("k", 10, 1.0, cost=3) == pytest.approx(3.0)


async def test_cost_above_capacity_never_allowed(clock):
    assert await rl._consume("k", 5, 1.0, cost=6) is not None


async def test_keys_are_independent(clock):
    assert await rl._consume("a", 1, 0.1) is None
    assert await rl._consume("a", 1, 0.1) is not None
    assert await rl._consume("b", 1, 0.1) is None


async def test_peek_does_not_charge(clock):
    assert await rl._peek("new", 5, 1.0) == 5
    assert await rl._peek("new", 5, 1.0) == 5
    await rl._consume("new", 5, 1.0, cost=2)
    assert await rl._peek("new", 5, 1.0) == 3


async def test_point_budget_charge_and_429_error(clock):
    b = rl.PointBudget("t", capacity=2, per_minute=60)
    req = make_request()
    await b.charge(req)
    await b.charge(req)
    with pytest.raises(AppError) as e:
        await b.charge(req)
    assert e.value.code == ENDPOINT_RATE_LIMITED
    assert int(e.value.headers["Retry-After"]) >= 1


async def test_point_budget_per_ip(clock):
    b = rl.PointBudget("t", capacity=1, per_minute=1)
    await b.charge(make_request("203.0.113.1"))
    await b.charge(make_request("203.0.113.2"))
    with pytest.raises(AppError):
        await b.charge(make_request("203.0.113.1"))


async def test_shared_budget_different_costs(clock):
    b = rl.PointBudget("stream", capacity=13, per_minute=1)
    req = make_request()
    await b.charge(req, 10)
    await b.charge(req, 3)
    assert await b.has_at_least(req, 1) is False
    with pytest.raises(AppError):
        await b.charge(req, 1)


async def test_has_at_least(clock):
    b = rl.PointBudget("h", capacity=5, per_minute=60)
    req = make_request()
    assert await b.has_at_least(req, 5) is True
    await b.charge(req, 3)
    assert await b.has_at_least(req, 2) is True
    assert await b.has_at_least(req, 3) is False


async def test_x_real_ip_trusted_only_from_private_peer(clock):
    b = rl.PointBudget("ip", capacity=1, per_minute=1)
    # public peer: spoofed header ignored -> same bucket
    await b.charge(make_request("203.0.113.1", {"x-real-ip": "1.1.1.1"}))
    with pytest.raises(AppError):
        await b.charge(make_request("203.0.113.1", {"x-real-ip": "2.2.2.2"}))
    # private peer (reverse proxy): header defines the client
    await b.charge(make_request("10.0.0.1", {"x-real-ip": "5.5.5.5"}))
    await b.charge(make_request("10.0.0.1", {"x-real-ip": "6.6.6.6"}))
    with pytest.raises(AppError):
        await b.charge(make_request("10.0.0.1", {"x-real-ip": "5.5.5.5"}))


async def test_rate_limit_dependency(clock):
    dep = rl.rate_limit("dep", capacity=1, per_minute=1)
    req = make_request()
    await dep(req)
    with pytest.raises(AppError) as e:
        await dep(req)
    assert e.value.code == ENDPOINT_RATE_LIMITED


async def test_check_global(clock, monkeypatch):
    monkeypatch.setattr(rl, "GLOBAL_CAPACITY", 2)
    req = make_request()
    await rl.check_global(req)
    await rl.check_global(req)
    with pytest.raises(AppError) as e:
        await rl.check_global(req)
    assert e.value.code == RATE_LIMITED
    assert "Retry-After" in e.value.headers


async def test_concurrent_consume_is_atomic(clock):
    res = await asyncio.gather(*[rl._consume("c", 5, 0.001) for _ in range(20)])
    assert sum(r is None for r in res) == 5


async def test_cleanup_loop_evicts_idle(monkeypatch):
    now = {"t": 0.0}
    monkeypatch.setattr(rl.time, "monotonic", lambda: now["t"])
    await rl._consume("old", 1, 1.0)
    now["t"] = rl._BUCKET_IDLE_TTL_SECONDS + 1
    await rl._consume("fresh", 1, 1.0)

    calls = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(rl.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await rl._cleanup_loop()
    assert "old" not in rl._buckets and "fresh" in rl._buckets
