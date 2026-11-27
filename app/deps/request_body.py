"""Request object parsing (docs/requests.md, objects/request.md, options.md).

    @router.post("")
    async def create(body: SingleBody[CamCreate] = Depends(single_body("cam", CamCreate))):
        cam_in = body.obj                      # validated pydantic instance
        body.options.partial_allowed           # bool (options > query)

    @router.post("/bulk")
    async def create_many(body: BulkBody[CamCreate] = Depends(bulk_body("cams", CamCreate, max_items=50))):
        body.items          # list[BulkItem]; item.obj (valid) or item.error (Error object dict, partial only)
        body.valid()        # [(index, obj)] ; body.invalid() -> failed_item dicts
        body.partial_allowed

Errors: 264 unsupported body format, 063 invalid JSON, 073 empty body,
269 validation error for invalid envelope/data (Field objects), 132 bulk limit exceeded,
066 invalid query for bad partial_allowed.
Atomic mode (partial_allowed false): any invalid item -> 269 validation error for the whole request.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from fastapi import Request
from pydantic import BaseModel, ValidationError

from app.core import codes
from app.core.errors import AppError
from app.core.introspection import declare
from app.core.validation import (
    field_error,
    fields_from_errors,
    prefixed_errors,
    py_type_name,
)

T = TypeVar("T")

DEFAULT_MAX_BULK = 100
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


@dataclass
class Options:
    partial_allowed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)  # custom options (unknown ones are kept, never required)


@dataclass
class SingleBody(Generic[T]):
    obj: T
    options: Options

    @property
    def partial_allowed(self) -> bool:
        return self.options.partial_allowed


@dataclass
class BulkItem(Generic[T]):
    index: int
    obj: T | None = None
    error: dict | None = None  # Error object (partial mode only)
    raw: Any = None


@dataclass
class BulkBody(Generic[T]):
    items: list[BulkItem[T]]
    options: Options

    @property
    def partial_allowed(self) -> bool:
        return self.options.partial_allowed

    def valid(self) -> list[tuple[int, T]]:
        return [(i.index, i.obj) for i in self.items if i.error is None]

    def invalid(self) -> list[dict]:
        return [{"id": i.index, "error": i.error} for i in self.items if i.error is not None]


async def read_envelope(request: Request, *, optional: bool = False) -> tuple[dict, Options]:
    """Parse and check the Request object -> (data dict, Options)."""
    # The ingress proxy is responsible for limiting body size before this full read.
    raw = await request.body()
    if not raw or not raw.strip():
        if optional:
            return {}, _options({}, request)
        raise AppError(codes.EMPTY_BODY, "The request body is empty")
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise AppError(
            codes.UNSUPPORTED_BODY_FORMAT, "Content-Type must be application/json",
            error_headers={"Content-Type": field_error("must be application/json", type="string", value=ctype or None)},
        )
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise AppError(codes.INVALID_JSON, "Invalid JSON body")
    if not isinstance(body, dict):
        raise AppError(codes.VALIDATION_ERROR, "Request body must be an object", fields={
            "data": field_error("is required", required=True, type="object")})
    data = body.get("data")
    if data is None:
        raise AppError(codes.VALIDATION_ERROR, "Some fields are invalid", fields={
            "data": field_error("is required", required=True, type="object")})
    if not isinstance(data, dict):
        raise AppError(codes.VALIDATION_ERROR, "Some fields are invalid", fields={
            "data": field_error("type mismatch", type="object", actual_type=py_type_name(data))})
    return data, _options(body.get("options"), request)


def _options(raw_opts: Any, request: Request) -> Options:
    if raw_opts is None:
        raw_opts = {}
    if not isinstance(raw_opts, dict):
        raise AppError(codes.VALIDATION_ERROR, "Some fields are invalid", fields={
            "options": field_error("type mismatch", type="object", actual_type=py_type_name(raw_opts))})
    partial: bool | None = None
    if "partial_allowed" in raw_opts:
        v = raw_opts["partial_allowed"]
        if not isinstance(v, bool):
            raise AppError(codes.VALIDATION_ERROR, "Some fields are invalid", fields={"options": {
                "error": "nested validation failed",
                "nested": {"partial_allowed": field_error("type mismatch", type="bool", actual_type=py_type_name(v))}}})
        partial = v
    if partial is None:  # options wins; query is the fallback
        q = request.query_params.get("partial_allowed")
        if q is None:
            partial = False
        elif q.lower() in _TRUE:
            partial = True
        elif q.lower() in _FALSE:
            partial = False
        else:
            raise AppError(codes.INVALID_QUERY, "Invalid query parameters", query={
                "partial_allowed": field_error("must be a boolean", type="bool", value=q)})
    extra = {k: v for k, v in raw_opts.items() if k != "partial_allowed"}
    return Options(partial_allowed=partial, extra=extra)


def _validate(schema: type[BaseModel], raw: Any, loc: tuple) -> tuple[Any, list[dict] | None]:
    try:
        return schema.model_validate(raw), None
    except ValidationError as exc:
        return None, prefixed_errors(exc.errors(), loc)


def single_body(key: str, schema: type[BaseModel], *, optional: bool = False):
    """Dependency for POST/PATCH single objects: data.<key> validated against `schema`."""
    async def _dep(request: Request) -> SingleBody:
        data, options = await read_envelope(request, optional=optional)
        if key not in data:
            raise AppError(codes.VALIDATION_ERROR, "Some fields are invalid", fields={
                key: field_error("is required", required=True, type="object")})
        raw = data[key]
        if not isinstance(raw, dict):
            raise AppError(codes.VALIDATION_ERROR, "Some fields are invalid", fields={
                key: field_error("type mismatch", type="object", actual_type=py_type_name(raw))})
        obj, errs = _validate(schema, raw, (key,))
        if errs:
            # keys are the object's own field names (strip the data key)
            raise AppError(codes.VALIDATION_ERROR, "Some fields are invalid", fields=fields_from_errors(errs, (key,)))
        return SingleBody(obj=obj, options=options)

    return _dep


def bulk_body(key_plural: str, schema: type[BaseModel], *, max_items: int = DEFAULT_MAX_BULK):
    """Dependency for bulk POST/PATCH: data.<key plural> is an array of objects."""
    async def _dep(request: Request) -> BulkBody:
        data, options = await read_envelope(request)
        if key_plural not in data:
            raise AppError(codes.VALIDATION_ERROR, "Some fields are invalid", fields={
                key_plural: field_error("is required", required=True, type="array")})
        arr = data[key_plural]
        if not isinstance(arr, list):
            raise AppError(codes.VALIDATION_ERROR, "Some fields are invalid", fields={
                key_plural: field_error("type mismatch", type="array", actual_type=py_type_name(arr))})
        if not arr:
            raise AppError(codes.EMPTY_BODY, "The request body is empty")
        if len(arr) > max_items:
            raise AppError(codes.BULK_LIMIT_EXCEEDED, f"At most {max_items} items per bulk request", fields={
                key_plural: field_error("too many items", type="array", max_length=max_items, actual_length=len(arr))})
        items: list[BulkItem] = []
        for i, raw in enumerate(arr):
            if not isinstance(raw, dict):
                errs = [{"type": "dict_type", "loc": (key_plural, i), "msg": "type mismatch", "input": raw}]
                obj = None
            else:
                obj, errs = _validate(schema, raw, (key_plural, i))
            if errs:
                if not options.partial_allowed:
                    raise AppError(codes.VALIDATION_ERROR, "Some fields are invalid", fields=fields_from_errors(errs))
                err_obj = {"error": "Validation error", "fields": fields_from_errors(errs, (key_plural, i))}
                items.append(BulkItem(index=i, error=err_obj, raw=raw))
            else:
                items.append(BulkItem(index=i, obj=obj, raw=raw))
        return BulkBody(items=items, options=options)

    declare(_dep, "bulk", "partial", max_bulk=max_items)
    return _dep
