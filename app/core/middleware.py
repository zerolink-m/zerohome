"""CursedMiddleware: request id/duration/version headers, global rate limit, OPTIONS,
last-resort 201 for unhandled exceptions. Pure ASGI (streaming-safe)."""
from __future__ import annotations

import logging
import time

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import codes
from app.core.context import new_request_id, request_id_var, started_var, warnings_var
from app.core.errors import AppError, error_response
from app.core.introspection import options_headers, options_info, route_info
from app.core.rate_limit import check_global
from app.core.response import CURSED_ENVELOPE_MARKER, CURSED_VERSION, respond, success

log = logging.getLogger(__name__)


class CursedMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rid = new_request_id()
        request_id_var.set(rid)
        started = time.perf_counter()
        started_var.set(started)
        warnings_var.set(None)
        extra: dict[str, str] = {}
        state = {"started": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["started"] = True
                headers = MutableHeaders(raw=message["headers"])
                headers["cursed-version"] = CURSED_VERSION
                headers["cursed-duration"] = f"{time.perf_counter() - started:.3f}"
                headers["cursed-request-id"] = rid
                status = message["status"]
                no_body = scope["method"] == "HEAD" or status in (204, 304)
                is_cursed_envelope = headers.get(CURSED_ENVELOPE_MARKER) == "1"
                if CURSED_ENVELOPE_MARKER in headers:
                    del headers[CURSED_ENVELOPE_MARKER]
                if is_cursed_envelope and not no_body:
                    # The marker is set only by the shared CursedJSON response builder.
                    if "cursed-code" in headers:
                        del headers["cursed-code"]
                else:
                    route = scope.get("route")
                    if route is not None and "streaming" in route_info(route)["functions"]:
                        code = codes.STREAMING
                    else:
                        code = codes.code_for_http_status(status)
                    headers["cursed-code"] = code
                for k, v in extra.items():
                    if k not in headers:
                        headers[k] = v
                route = scope.get("route")
                if route is not None and getattr(route, "deprecated", False):
                    headers["cursed-deprecation"] = "true"
            await send(message)

        request = Request(scope, receive)
        try:
            try:
                extra.update(await check_global(request))
            except AppError as exc:
                await error_response(exc)(scope, receive, send_wrapper)
                return

            if scope["method"] == "OPTIONS":
                has_contract = getattr(scope["app"].state, "cursed_has_contract", None)
                if has_contract and has_contract(scope["path"]):
                    await self.app(scope, receive, send_wrapper)
                    return
                info = options_info(scope)
                if info is None:
                    exc = AppError(codes.ROUTE_NOT_FOUND, "Route not found")
                    await error_response(exc)(scope, receive, send_wrapper)
                    return
                resp = success({"endpoint": {
                    "path": scope["path"],
                    "methods": info["methods"],
                    "supported_functions": info["functions"],
                }}, headers=options_headers(info))
                await resp(scope, receive, send_wrapper)
                return

            await self.app(scope, receive, send_wrapper)
        except Exception:  # noqa: BLE001 - last resort, see docstring
            log.exception("unhandled exception on %s %s", scope.get("method"), scope.get("path"))
            if state["started"]:
                raise
            await respond(
                codes.SERVER_ERROR, error={"error": "Internal server error"},
            )(scope, receive, send_wrapper)
