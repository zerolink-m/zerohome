"""CursedJSON response builders (docs/cursedapi/docs/objects/envelope.md & co).

Every JSON response is the Envelope {code, code_detail, error, metadata, data}.
Router cheat-sheet:

    return single("cam", cam_dict)                       # 000, data={"cam": {...}}
    return created("cam", cam_dict)                      # 001 / HTTP 201
    return edited("cam", cam_dict)                       # 003  (changed=False -> 005)
    return deleted("cam", {"id": 1})                     # 007
    return listing("cams", items, query=q, total=total)  # 000 / 012 if empty, data.settings
    return bulk("cams", ok_items, failed, op="create", partial=body.partial_allowed)
    return success({"token": "..."}, code=AUTHORIZED)    # virtual endpoints
    return empty()                                       # data={}
    raise AppError(OBJECT_NOT_FOUND, "Camera not found") # errors (app.core.errors)
"""
from __future__ import annotations

import math
import time
from typing import Any, Iterable, Mapping

from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse

from app.core import codes
from app.core.context import add_warning as add_warning  # noqa: F401  (re-export)
from app.core.context import elapsed, get_request_id, warnings_var

CURSED_VERSION = "1.1"


def build_metadata(
    extra: Mapping[str, Any] | None = None,
    *,
    page: dict | None = None,
    expand: list[str] | None = None,
    deprecated: bool | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "request_id": get_request_id(),
        "generated_at": int(time.time()),
    }
    dur = elapsed()
    if dur is not None:
        meta["duration"] = round(dur, 6)
    meta["cursed_version"] = CURSED_VERSION
    if deprecated:
        meta["deprecated"] = True
    if page is not None:
        meta["page"] = page
    if expand:
        meta["expand"] = list(expand)
    warns = warnings_var.get()
    if warns:
        meta["warnings"] = list(warns)
    if extra:
        meta.update(extra)
    return meta


def envelope(
    code: str,
    *,
    data: Any = None,
    error: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """The five-field Envelope. All keys are always present."""
    return {
        "code": code,
        "code_detail": detail if detail is not None else codes.code_detail(code),
        "error": dict(error) if error else {},
        "metadata": metadata if metadata is not None else build_metadata(),
        "data": {} if data is None else data,
    }


def respond(
    code: str,
    *,
    data: Any = None,
    error: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    status_code: int | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = envelope(code, data=data, error=error, metadata=metadata)
    return JSONResponse(
        content=jsonable_encoder(body),
        status_code=status_code if status_code is not None else codes.http_status(code),
        headers=dict(headers) if headers else None,
    )


# --- success -------------------------------------------------------------------

def success(
    data: Any = None,
    *,
    code: str = codes.SUCCESS,
    status_code: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Generic success. `data` is the Data object (a dict) or None -> {}."""
    meta = build_metadata(metadata) if metadata else None
    return respond(code, data=data, metadata=meta, status_code=status_code, headers=headers)


def empty(code: str = codes.SUCCESS, **kw) -> JSONResponse:
    return success({}, code=code, **kw)


def single(key: str, obj: Any, *, code: str = codes.SUCCESS, expand: list[str] | None = None, **kw) -> JSONResponse:
    meta = kw.pop("metadata", None)
    m = build_metadata(meta, expand=expand)
    return respond(code, data={key: obj}, metadata=m, **kw)


def created(key: str, obj: Any, **kw) -> JSONResponse:
    return single(key, obj, code=codes.CREATED, **kw)


def edited(key: str, obj: Any, *, changed: bool = True, **kw) -> JSONResponse:
    return single(key, obj, code=codes.EDITED if changed else codes.NOT_CHANGED, **kw)


def deleted(key: str | None = None, obj: Any = None, **kw) -> JSONResponse:
    """007; data = {key: obj} when an object is given, else {}."""
    return success({key: obj} if key and obj is not None else {}, code=codes.DELETED, **kw)


def accepted(data: Any = None, **kw) -> JSONResponse:
    return success(data, code=codes.ACCEPTED, **kw)


# --- lists ---------------------------------------------------------------------

def default_settings(total: int, *, limit: int = 25, offset: int = 0) -> dict[str, Any]:
    return {
        "total": total, "limit": limit, "offset": offset, "order": "ascending",
        "sort_by": None, "search": None, "search_method": None, "search_column": None,
    }


def page_info(total: int, limit: int, offset: int) -> dict[str, int]:
    pages = max(1, math.ceil(total / limit)) if limit > 0 else 1
    return {"current": offset // limit + 1 if limit > 0 else 1, "pages": pages}


def listing(
    key: str,
    items: Iterable[Any],
    *,
    query: Any = None,
    total: int | None = None,
    settings: Mapping[str, Any] | None = None,
    expand: list[str] | None = None,
    **kw,
) -> JSONResponse:
    """List response: data = {settings: {...}, <key plural>: [...]}. Code 000, or 012
    when the list is empty. `query` is an app.deps.list_query.ListQuery (gives the
    settings echo + expand); otherwise pass `settings` or just `total`."""
    items = list(items)
    total = len(items) if total is None else total
    if settings is None:
        settings = query.settings(total) if query is not None else default_settings(total)
    if expand is None and query is not None:
        expand = list(getattr(query, "expand", []) or [])
    meta = build_metadata(
        kw.pop("metadata", None),
        page=page_info(total, settings["limit"], settings["offset"]),
        expand=expand,
    )
    code = codes.SUCCESS if items else codes.EMPTY_RESULT
    return respond(code, data={"settings": dict(settings), key: items}, metadata=meta, **kw)


def bulk_listing(key: str, items: Iterable[Any], *, requested: int | None = None, **kw) -> JSONResponse:
    """Bulk GET (/bulk/{ids}): settings.total = number of requested ids; no limit/offset."""
    items = list(items)
    total = len(items) if requested is None else requested
    s = default_settings(total, limit=max(total, 1), offset=0)
    return listing(key, items, settings=s, total=total, **kw)


# --- bulk ----------------------------------------------------------------------

_BULK_CODES = {
    "create": (codes.CREATED, codes.PARTIALLY_CREATED),
    "edit": (codes.EDITED, codes.PARTIALLY_EDITED),
    "delete": (codes.DELETED, codes.PARTIALLY_DELETED),
}


class _FailedItem(dict):
    """`failed` element; `.code` (not serialized) remembers the error code of the cause."""

    code: str | None = None


def failed_item(id_or_index: Any, error: Any, code: str | None = None) -> dict[str, Any]:
    """Element of Bulk object `failed`. `id_or_index`: object id (PATCH/DELETE) or the
    positional index of the request element (POST). `error`: message, error mapping or
    an AppError (its error_object and code are used). `code` (optional) is the real
    cause code, used by bulk() to pick the all-failed response code."""
    if hasattr(error, "error_object"):
        code = code or getattr(error, "code", None)
        err = error.error_object()
    else:
        err = {"error": error} if isinstance(error, str) else dict(error)
    item = _FailedItem(id=id_or_index, error=err)
    item.code = code
    return item


def bulk(
    key: str,
    succeeded: list[Any],
    failed: list[dict] | None = None,
    *,
    op: str = "create",
    partial: bool = False,
    all_failed_code: str | None = None,
    **kw,
) -> JSONResponse:
    """Result of a bulk write.

    * no failures -> full success (001/003/007), data = {key: [objects]}
    * failures + partial=True + some succeeded -> 002/004/008, Bulk object
      data = {key: {"succeeded": [...], "failed": [...]}}
    * failures + nothing succeeded -> error carrying the Bulk object in data.
      Code: explicit `all_failed_code`; else the common code of all failures (see
      failed_item(code=/AppError)) when they agree; else 109 (the spec lets the
      server pick this code)
    * failures + partial=False -> caller must have rejected atomically before
      applying anything; this helper treats it as all_failed.
    """
    from app.core.errors import AppError  # local: errors imports response

    failed = failed or []
    full, part = _BULK_CODES[op]
    if not failed:
        return respond(full, data={key: succeeded}, metadata=build_metadata(kw.pop("metadata", None)), **kw)
    bulk_obj = {"succeeded": succeeded, "failed": failed}
    if partial and succeeded:
        return respond(part, data={key: bulk_obj}, metadata=build_metadata(kw.pop("metadata", None)), **kw)
    first = failed[0].get("error", {}).get("error", "Bulk operation failed")
    if all_failed_code is None:
        causes = {getattr(f, "code", None) for f in failed}
        all_failed_code = causes.pop() if len(causes) == 1 and None not in causes else codes.VALIDATION_ERROR
    raise AppError(all_failed_code, first, data={key: bulk_obj})
