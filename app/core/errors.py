"""AppError + exception handlers producing CursedJSON error envelopes."""
from __future__ import annotations

import logging
from typing import Any, Mapping

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core import codes
from app.core.codes import SERVER_ERROR  # noqa: F401  (legacy import path)
from app.core.response import respond, success
from app.core.validation import split_request_validation

log = logging.getLogger(__name__)

_RATE_CODES = {codes.RATE_LIMITED, codes.LOGIN_RATE_LIMITED, codes.ENDPOINT_RATE_LIMITED, codes.UPLOAD_RATE_LIMITED}


class AppError(Exception):
    """Raise anywhere (routers, deps, services). HTTP status comes from the code table.

        raise AppError(OBJECT_NOT_FOUND, "Camera not found")
        raise AppError(VALIDATION_ERROR, "Bad data", fields={"name": field_error("is required", required=True)})
        raise AppError(INVALID_QUERY, "Bad limit", query={"limit": field_error("must be int")}, hint="...")

    `headers` = extra HTTP response headers (legacy positional arg);
    `error_headers` = the Error object's `headers` section (per-header Field objects).
    """

    def __init__(
        self,
        code: str,
        message: str | None = None,
        headers: Mapping[str, str] | None = None,
        *,
        fields: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        path: Mapping[str, Any] | None = None,
        error_headers: Mapping[str, Any] | None = None,
        hint: str | None = None,
        docs: str | None = None,
        data: Any = None,
        status_code: int | None = None,
    ):
        super().__init__(message or codes.code_detail(code))
        self.code = code
        self.message = message or codes.code_detail(code)
        self.headers = dict(headers) if headers else None
        self.fields = fields
        self.query = query
        self.path = path
        self.error_headers = error_headers
        self.hint = hint
        self.docs = docs
        self.data = data
        self._status_code = status_code

    @property
    def http_status(self) -> int:
        return self._status_code if self._status_code is not None else codes.http_status(self.code)

    def error_object(self) -> dict[str, Any]:
        obj: dict[str, Any] = {"error": self.message}
        for key, val in (
            ("fields", self.fields), ("query", self.query), ("path", self.path),
            ("headers", self.error_headers), ("hint", self.hint), ("docs", self.docs),
        ):
            if val:
                obj[key] = dict(val) if isinstance(val, Mapping) else val
        return obj


def _response_headers(exc: AppError) -> dict[str, str]:
    h = dict(exc.headers or {})
    low = {k.lower() for k in h}
    if exc.code in _RATE_CODES and "retry-after" in low and "cursed-retry-after" not in low:
        h["cursed-retry-after"] = next(v for k, v in h.items() if k.lower() == "retry-after")
    if exc.http_status == 401 and "www-authenticate" not in low:
        h["WWW-Authenticate"] = "Bearer"
    return h


def error_response(exc: AppError) -> JSONResponse:
    return respond(
        exc.code,
        data=exc.data,
        error=exc.error_object(),
        status_code=exc.http_status,
        headers=_response_headers(exc),
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return error_response(exc)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    info = split_request_validation(exc.errors())
    code = info.pop("code")
    message = info.pop("error")
    return error_response(AppError(
        code, message,
        fields=info.get("fields"), query=info.get("query"),
        path=info.get("path"), error_headers=info.get("headers"),
    ))


_HTTP_TO_CODE: dict[int, str] = {
    400: codes.BAD_REQUEST, 401: codes.TOKEN_ERROR, 402: codes.PAYMENT_REQUIRED, 403: codes.FORBIDDEN,
    404: codes.NOT_FOUND, 405: codes.METHOD_NOT_ALLOWED, 406: codes.NOT_ACCEPTABLE,
    408: codes.REQUEST_TIMEOUT, 409: codes.CONFLICT, 410: codes.GONE, 411: codes.LENGTH_REQUIRED,
    412: codes.PRECONDITION_FAILED, 413: codes.PAYLOAD_TOO_LARGE, 414: codes.URI_TOO_LONG,
    415: codes.UNSUPPORTED_BODY_FORMAT, 416: codes.RANGE_NOT_SATISFIABLE, 422: codes.VALIDATION_ERROR,
    423: codes.LOCKED, 428: codes.PRECONDITION_REQUIRED, 429: codes.RATE_LIMITED,
    451: codes.LEGAL_REASONS, 500: codes.SERVER_ERROR, 501: codes.NOT_IMPLEMENTED,
    502: codes.BAD_GATEWAY, 503: codes.SERVICE_UNAVAILABLE, 504: codes.GATEWAY_TIMEOUT,
}


def code_for_http_status(status_code: int, detail: Any = None) -> str:
    # Starlette's router raises 404 "Not Found" for an unknown path -> 083 Route not found.
    if status_code == 404 and detail in (None, "Not Found"):
        return codes.ROUTE_NOT_FOUND
    if status_code in _HTTP_TO_CODE:
        return _HTTP_TO_CODE[status_code]
    if status_code >= 500:
        return codes.SERVER_ERROR
    return codes.BAD_REQUEST


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = code_for_http_status(exc.status_code, exc.detail)
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else codes.code_detail(code)
    return error_response(AppError(
        code, message, dict(exc.headers) if exc.headers else None, status_code=exc.status_code,
    ))


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled exception on %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(AppError(codes.SERVER_ERROR, "Internal server error"))
