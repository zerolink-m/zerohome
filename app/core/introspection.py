"""OPTIONS / `cursed-supported-functions` support (docs/headers.md).

Each route advertises what it supports. Two ways to declare it:

    @router.get("", ...)
    @supports("limit", "offset", "sort_by", "order", "search", "search_column",
              "search_method", "multi_search", "pagination", max_limit=255)
    async def list_cams(...): ...

or implicitly: dependencies built by app.deps.list_query.list_query(...) and
app.deps.request_body.single_body/bulk_body carry the declaration themselves, so a
route using them is introspected automatically.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from starlette.routing import Match

CANONICAL_FUNCTIONS = (
    "limit", "offset", "sort_by", "order", "search", "search_method", "search_column",
    "expand", "bulk", "multi_search", "partial", "streaming", "pagination",
)


def _check(names: Iterable[str]) -> frozenset[str]:
    out = set()
    for n in names:
        if n not in CANONICAL_FUNCTIONS and not n.startswith("x-"):
            raise ValueError(f"custom cursed function must be prefixed with 'x-': {n!r}")
        out.add(n)
    return frozenset(out)


def declare(fn: Callable, *functions: str, **meta: Any) -> Callable:
    """Attach a declaration to a callable (endpoint or dependency)."""
    fn.cursed_functions = _check(functions) | getattr(fn, "cursed_functions", frozenset())  # type: ignore[attr-defined]
    cm = dict(getattr(fn, "cursed_meta", {}))
    cm.update({k: v for k, v in meta.items() if v is not None})
    fn.cursed_meta = cm  # type: ignore[attr-defined]
    return fn


def supports(*functions: str, **meta: Any):
    """Decorator for endpoints. meta keys: max_limit, max_bulk, cache(bool), streaming(bool)."""
    def deco(fn: Callable) -> Callable:
        return declare(fn, *functions, **meta)
    return deco


def _walk(dependant) -> Iterable[Callable]:
    for dep in getattr(dependant, "dependencies", []):
        if dep.call is not None:
            yield dep.call
        yield from _walk(dep)


def route_info(route) -> dict[str, Any]:
    functions: set[str] = set()
    meta: dict[str, Any] = {}
    calls: list[Callable] = []
    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        calls.append(endpoint)
    dependant = getattr(route, "dependant", None)
    if dependant is not None:
        calls.extend(_walk(dependant))
    auth = "off"
    for call in calls:
        functions |= set(getattr(call, "cursed_functions", ()))
        meta.update(getattr(call, "cursed_meta", {}))
        if getattr(call, "__module__", "") == "app.deps.auth":
            qs = call.__name__.endswith("_qs") or "resolve" in call.__name__
            auth = "bearer,token" if qs else ("bearer" if auth == "off" else auth)
    return {"functions": functions, "meta": meta, "auth": auth}


def _flatten(routes) -> Iterable[Any]:
    """Yield concrete routes. Newer FastAPI wraps included routers in lazy
    `_IncludedRouter` objects exposing `effective_candidates()`; flatten them by duck typing."""
    for r in routes:
        if hasattr(r, "effective_candidates"):
            yield from _flatten(r.effective_candidates())
        else:
            yield r


def match_routes(scope) -> list:
    """Concrete routes whose path matches the request path (any method)."""
    path = scope["path"]
    out = []
    for route in _flatten(scope["app"].router.routes):
        rx = getattr(route, "path_regex", None)
        if rx is not None and rx.match(path):
            out.append(route)
    return out


def options_info(scope) -> dict[str, Any] | None:
    routes = match_routes(scope)
    if not routes:
        return None
    methods: set[str] = set()
    functions: set[str] = set()
    meta: dict[str, Any] = {}
    auth = "off"
    for r in routes:
        methods |= set(getattr(r, "methods", None) or ())
        info = route_info(r)
        functions |= info["functions"]
        meta.update(info["meta"])
        if info["auth"] != "off":
            auth = info["auth"]
    methods.add("OPTIONS")
    return {"methods": sorted(methods), "functions": sorted(functions), "meta": meta, "auth": auth}


def options_headers(info: dict[str, Any]) -> dict[str, str]:
    fn = set(info["functions"])
    meta = info["meta"]
    h = {
        "Allow": ",".join(info["methods"]),
        "cursed-supported-functions": ",".join(info["functions"]),
        "cursed-version": "1.1",
        "cursed-bulk": "on" if "bulk" in fn else "off",
        "cursed-paging": "on" if ("pagination" in fn or "offset" in fn) else "off",
        "cursed-streaming": "on" if "streaming" in fn else "off",
        "cursed-search": "on" if "search" in fn else "off",
        "cursed-auth": info["auth"],
        "cursed-accept": "application/json",
        "cursed-cache": "on" if meta.get("cache") else "off",
    }
    if "bulk" in fn and meta.get("max_bulk"):
        h["cursed-max-bulk"] = str(meta["max_bulk"])
    else:
        h["cursed-max-bulk"] = "off"
    if meta.get("max_limit"):
        h["cursed-paging-max-limit"] = str(meta["max_limit"])
    return h
