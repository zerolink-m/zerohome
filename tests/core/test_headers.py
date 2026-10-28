from app.core import rate_limit as rl


async def test_rate_limit_headers_on_success(c):
    r = await c.get("/v1/health")
    assert r.headers["cursed-rate-limit-limit"] == str(rl.GLOBAL_CAPACITY)
    assert int(r.headers["cursed-rate-limit-remaining"]) < rl.GLOBAL_CAPACITY
    assert int(r.headers["cursed-rate-limit-reset"]) > 0
    assert r.headers["cursed-rate-limit-policy"].startswith(f"{rl.GLOBAL_CAPACITY};w=")


async def test_global_rate_limit_114(c, monkeypatch):
    monkeypatch.setattr(rl, "GLOBAL_CAPACITY", 2)
    await c.get("/v1/health")
    await c.get("/v1/health")
    r = await c.get("/v1/health")
    assert r.status_code == 429 and r.json()["code"] == "114"
    assert r.headers["cursed-rate-limit-remaining"] == "0"
    assert int(r.headers["cursed-retry-after"]) >= 1 and "retry-after" in r.headers
    assert r.headers["cursed-request-id"] and r.headers["cursed-version"] == "1.1"


async def test_endpoint_rate_limit_173(c, mk_user):
    await mk_user("a@x.io")
    for _ in range(3):
        r = await c.post("/v1/auth/register", json={})
    # register bucket is 3/min: forth request is denied with 173
    r = await c.post("/v1/auth/register", json={})
    assert r.status_code == 429 and r.json()["code"] == "173"
    assert "cursed-retry-after" in r.headers


async def test_deprecation_header(c):
    r = await c.get("/t/old")
    assert r.headers["cursed-deprecation"] == "true"
    assert "cursed-deprecation" not in (await c.get("/v1/health")).headers


async def test_options_list_endpoint(c):
    r = await c.options("/t/users")
    assert r.status_code == 200 and r.json()["code"] == "000"
    fns = r.headers["cursed-supported-functions"].split(",")
    for f in ("limit", "offset", "sort_by", "order", "search", "search_column", "search_method", "multi_search", "expand", "pagination"):
        assert f in fns
    assert r.headers["cursed-version"] == "1.1"
    assert r.headers["cursed-paging-max-limit"] == "255" and r.headers["cursed-search"] == "on"
    assert "GET" in r.headers["allow"] and "OPTIONS" in r.headers["allow"]


async def test_options_bulk_and_custom(c):
    r = await c.options("/t/items/bulk")
    fns = r.headers["cursed-supported-functions"].split(",")
    assert "bulk" in fns and "partial" in fns
    assert r.headers["cursed-bulk"] == "on" and r.headers["cursed-max-bulk"] == "3"
    r = await c.options("/t/old")
    assert r.headers["cursed-supported-functions"] == "x-geo-filter"
    assert r.headers["cursed-search"] == "off"


async def test_options_unknown_path(c):
    r = await c.options("/v1/zzz")
    assert r.status_code == 404 and r.json()["code"] == "083"


async def test_unsupported_function_181(c):
    # /t/users supports search; build a list endpoint without it via the dependency directly
    from fastapi import APIRouter, Depends
    from app.deps.list_query import list_query
    from app.models.user import User
    r = APIRouter()

    @r.get("/t2/users")
    async def u(q=Depends(list_query(model=User, sortable=["id"]))):
        return {}

    c._transport.app.include_router(r)
    resp = await c.get("/t2/users?search=a&search_column=name")
    assert resp.json()["code"] == "181" and resp.status_code == 400
    resp = await c.get("/t2/users?expand=a")
    assert resp.json()["code"] == "215" and resp.status_code == 501
