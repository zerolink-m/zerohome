"""Per-request context (request id, start time, extra metadata).

Set by app.core.middleware.CursedMiddleware; read by app.core.response when it
builds the envelope. Outside a request (unit tests, background tasks) values
fall back to fresh ones.
"""
from __future__ import annotations

import time
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("cursed_request_id", default=None)
started_var: ContextVar[float | None] = ContextVar("cursed_started", default=None)
warnings_var: ContextVar[list[str] | None] = ContextVar("cursed_warnings", default=None)


def new_request_id() -> str:
    return str(uuid.uuid4())


def get_request_id() -> str:
    rid = request_id_var.get()
    if rid is None:
        rid = new_request_id()
        request_id_var.set(rid)
    return rid


def elapsed() -> float | None:
    started = started_var.get()
    return None if started is None else time.perf_counter() - started


def add_warning(text: str) -> None:
    """Queue a non-blocking warning for `metadata.warnings` of the current response."""
    cur = warnings_var.get()
    if cur is None:
        cur = []
        warnings_var.set(cur)
    cur.append(text)
