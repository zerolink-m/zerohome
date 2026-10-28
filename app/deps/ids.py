"""Path segment `{ids}` (bulk GET/PATCH/DELETE: `/bulk/1,2,5`) and the bodiless
`?partial_allowed=` query flag.

parse_ids(raw, max_items=100) -> list[int]
    * order kept, duplicates silently dropped (the list is a set of objects)
    * empty / non-integer position -> 109 with error.path.ids (a malformed path
      parameter is a validation error of the path; 118 is reserved for the query
      string, 040 is a generic fallback)
    * more than max_items distinct ids -> 146 with error.path.ids

partial_query() -> bool   (FastAPI dependency)
    reads `?partial_allowed=`; absent -> False; not a boolean -> 118 (same as
    request_body options).
"""
from __future__ import annotations

from fastapi import Request

from app.core import codes
from app.core.errors import AppError
from app.core.validation import field_error

DEFAULT_MAX_IDS = 100
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def parse_ids(raw: str, max_items: int = DEFAULT_MAX_IDS) -> list[int]:
    ids: list[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not (tok.isascii() and tok.isdigit()):
            raise AppError(codes.VALIDATION_ERROR, "Some path parameters are invalid", path={
                "ids": field_error("must be a comma-separated list of integers", type="string", value=raw)})
        ids.append(int(tok))
    ids = list(dict.fromkeys(ids))
    if len(ids) > max_items:
        raise AppError(codes.BULK_LIMIT_EXCEEDED, f"At most {max_items} ids per bulk request", path={
            "ids": field_error("too many ids", max_length=max_items, actual_length=len(ids))})
    return ids


def partial_query(request: Request) -> bool:
    q = request.query_params.get("partial_allowed")
    if q is None:
        return False
    low = q.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise AppError(codes.INVALID_QUERY, "Invalid query parameters", query={
        "partial_allowed": field_error("must be a boolean", type="bool", value=q)})
