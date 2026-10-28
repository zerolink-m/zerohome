"""Field objects (docs/objects/field.md) and conversion of pydantic/FastAPI
validation errors into cursedapi error objects."""
from __future__ import annotations

from typing import Any

from app.core import codes

# Keys whose values must never be echoed back in `value`.
SENSITIVE_KEYS = {"password", "old_password", "new_password", "token", "secret", "api_key", "key", "authorization"}

_PY_TYPES = {
    "str": "string", "int": "int", "float": "float", "bool": "bool",
    "list": "array", "tuple": "array", "set": "array", "dict": "object", "NoneType": "null",
    "bytes": "string",
}


def py_type_name(value: Any) -> str:
    return _PY_TYPES.get(type(value).__name__, type(value).__name__)


def field_error(
    error: str,
    *,
    required: bool | None = None,
    type: str | None = None,  # noqa: A002 - spec key name
    actual_type: str | None = None,
    value: Any = None,
    has_value: bool = False,
    min_length: int | None = None,
    max_length: int | None = None,
    actual_length: int | None = None,
    min: int | float | None = None,  # noqa: A002
    max: int | float | None = None,  # noqa: A002
    enum: list[str] | None = None,
    pattern: str | None = None,
    format: str | None = None,  # noqa: A002
    immutable: bool | None = None,
    unknown: bool | None = None,
    nested: dict | None = None,
    items: dict | None = None,
) -> dict[str, Any]:
    """Build a Field object; None-valued keys are omitted. Pass has_value=True to
    include `value` even when it is None."""
    out: dict[str, Any] = {"error": error}
    for key, val in (
        ("required", required), ("type", type), ("actual_type", actual_type),
        ("min_length", min_length), ("max_length", max_length), ("actual_length", actual_length),
        ("min", min), ("max", max), ("enum", enum), ("pattern", pattern), ("format", format),
        ("immutable", immutable), ("unknown", unknown), ("nested", nested), ("items", items),
    ):
        if val is not None:
            out[key] = val
    if value is not None or has_value:
        out["value"] = value
    return out


def _safe_value(name: str, value: Any) -> tuple[bool, Any]:
    if name.lower() in SENSITIVE_KEYS:
        return False, None
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 200:
            return True, value[:200] + "..."
        return True, value
    return False, None


def _expected_type(etype: str) -> tuple[str | None, str | None]:
    """pydantic error type -> (field type, format)."""
    head = etype.split("_")[0]
    if etype.startswith("uuid"):
        return "string", "uuid"
    if etype.startswith("url"):
        return "string", "url"
    if etype.startswith("datetime"):
        return "datetime", None
    if etype.startswith("date"):
        return "date", None
    if etype in ("list_type", "tuple_type", "set_type", "frozen_set_type", "too_short", "too_long"):
        return "array", None
    if etype in ("dict_type", "model_type", "model_attributes_type", "model_fields_type"):
        return "object", None
    if head in ("string",):
        return "string", None
    if head in ("int",):
        return "int", None
    if head in ("float", "finite"):
        return "float", None
    if head in ("bool",):
        return "bool", None
    return None, None


def _literal_values(ctx: dict) -> list[str] | None:
    expected = ctx.get("expected")
    if not expected:
        return None
    return [p.strip().strip("'\"") for p in str(expected).split(",") if p.strip()]


def _field_from_error(err: dict, name: str) -> dict[str, Any]:
    etype: str = err.get("type", "")
    ctx: dict = err.get("ctx") or {}
    inp = err.get("input")
    msg = err.get("msg", "invalid value")

    if etype == "missing":
        return field_error("is required", required=True)

    kw: dict[str, Any] = {}
    exp_type, fmt = _expected_type(etype)
    if exp_type:
        kw["type"] = exp_type
    if fmt:
        kw["format"] = fmt
    text = msg
    if etype.endswith("_type") or etype.endswith("_parsing") or etype.startswith(("uuid", "url", "datetime", "date")):
        kw["actual_type"] = py_type_name(inp)
        if etype.endswith("_type"):
            text = "type mismatch"
    if etype in ("string_too_short", "too_short"):
        kw["min_length"] = ctx.get("min_length")
        try:
            kw["actual_length"] = len(inp)
        except TypeError:
            pass
        text = "too short"
    elif etype in ("string_too_long", "too_long"):
        kw["max_length"] = ctx.get("max_length")
        try:
            kw["actual_length"] = len(inp)
        except TypeError:
            pass
        text = "too long"
    elif etype in ("greater_than", "greater_than_equal", "less_than", "less_than_equal"):
        for ck, out_key in (("gt", "min"), ("ge", "min"), ("lt", "max"), ("le", "max")):
            if ck in ctx:
                kw[out_key] = ctx[ck]
        text = "out of range"
    elif etype in ("literal_error", "enum"):
        kw["enum"] = _literal_values(ctx)
        text = "invalid enum value"
    elif etype == "string_pattern_mismatch":
        kw["pattern"] = ctx.get("pattern")
        text = "does not match the pattern"
    elif etype == "extra_forbidden":
        kw["unknown"] = True
        text = "unknown field"
    elif etype == "value_error" and "email" in msg.lower():
        kw["format"] = "email"
        kw["type"] = "string"
        text = "invalid format"

    show, value = _safe_value(name, inp)
    if show and etype not in ("extra_forbidden",):
        kw["value"] = value
        kw["has_value"] = True
    return field_error(text, **kw)


def _attach(container: dict, path: list, field: dict) -> None:
    name = str(path[0])
    rest = path[1:]
    if not rest:
        container.setdefault(name, field)
        return
    node = container.setdefault(name, {"error": "nested validation failed"})
    nxt = rest[0]
    if isinstance(nxt, int):
        node["error"] = f"invalid item at index {nxt}"
        node.setdefault("type", "array")
        items = node.setdefault("items", {"index": nxt})
        if items["index"] != nxt:
            return  # report the first failing item only
        sub = rest[1:]
        if not sub:
            items.setdefault("fields", field)
        else:
            holder = items.setdefault("fields", {"error": "nested validation failed"})
            _attach(holder.setdefault("nested", {}), sub, field)
    else:
        node["error"] = "nested validation failed"
        _attach(node.setdefault("nested", {}), rest, field)


def fields_from_errors(errors: list[dict], prefix: tuple = ()) -> dict[str, Any]:
    """pydantic `errors()` list -> {field_name: Field object}. `prefix` is a loc
    prefix to strip (e.g. ("body",)); errors with an empty remaining loc are
    reported under the key "data"."""
    out: dict[str, Any] = {}
    for err in errors:
        loc = list(err.get("loc", ()))
        if tuple(loc[: len(prefix)]) == tuple(prefix):
            loc = loc[len(prefix):]
        if not loc:
            loc = ["data"]
        name = str(loc[-1]) if not isinstance(loc[-1], int) else str(loc[0])
        _attach(out, loc, _field_from_error(err, name))
    return out


def prefixed_errors(errors: list[dict], path: tuple) -> list[dict]:
    """Prepend `path` to every error loc (to report errors of data.<key> under it)."""
    return [{**e, "loc": tuple(path) + tuple(e.get("loc", ()))} for e in errors]


def split_request_validation(errors: list[dict]) -> dict[str, Any]:
    """FastAPI RequestValidationError.errors() -> {code, error, fields/query/path/headers}.

    Rules: invalid JSON -> 117; whole body missing -> 121; only query problems ->
    160 (all missing) or 118; everything else -> 109.
    """
    body, query, path, headers = [], [], [], []
    for e in errors:
        where = (e.get("loc") or ("body",))[0]
        {"query": query, "path": path, "header": headers, "cookie": headers}.get(where, body).append(e)

    if any(e.get("type") == "json_invalid" for e in errors):
        return {"code": codes.INVALID_JSON, "error": "Invalid JSON body"}
    if any(e.get("type") == "missing" and tuple(e.get("loc", ())) == ("body",) for e in errors):
        return {"code": codes.EMPTY_BODY, "error": "The request body is empty"}

    out: dict[str, Any] = {}
    if body:
        out["fields"] = fields_from_errors(body, ("body",))
    if query:
        out["query"] = fields_from_errors(query, ("query",))
    if path:
        out["path"] = fields_from_errors(path, ("path",))
    if headers:
        out["headers"] = fields_from_errors(headers, ("header",)) if any(
            (e.get("loc") or [""])[0] == "header" for e in headers
        ) else fields_from_errors(headers, ("cookie",))

    if query and not (body or path or headers):
        all_missing = all(e.get("type") == "missing" for e in query)
        out["code"] = codes.QUERY_REQUIRED if all_missing else codes.INVALID_QUERY
        out["error"] = "A required query parameter is missing" if all_missing else "Invalid query parameters"
    else:
        out["code"] = codes.VALIDATION_ERROR
        out["error"] = "Some fields are invalid" if body and not (query or path or headers) else "Request validation failed"
    return out
