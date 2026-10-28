"""Tuya IoT Cloud: utils (split from legacy tuya_client.py)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable,
    Literal,
    Mapping,
    Sequence,
)

import httpx
from pydantic import BaseModel, ConfigDict, Field


from app.services.tuya.constants import _EMPTY_BODY_SHA256, _TOKEN_REFRESH_LEEWAY_S

# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------


@dataclass
class _TokenState:
    """In-memory token cache guarded by an asyncio lock."""

    access_token: str | None = None
    refresh_token: str | None = None
    uid: str | None = None
    #: ``time.monotonic()`` at which the token becomes invalid.
    expire_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_valid(self, leeway: float = _TOKEN_REFRESH_LEEWAY_S) -> bool:
        return bool(self.access_token) and (time.monotonic() + leeway) < self.expire_at

    def clear(self) -> None:
        self.access_token = None
        self.refresh_token = None
        self.uid = None
        self.expire_at = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """Convenience helper: MD5 hexdigest for the ``password`` field of the
    user-registration endpoint.  Tuya requires the password to be pre-hashed
    by the caller.
    """
    return hashlib.md5(plain.encode("utf-8")).hexdigest()


def _mask(secret: str | None, keep: int = 4) -> str:
    """Return a masked version of a secret for safe logging."""
    if not secret:
        return "<none>"
    if len(secret) <= keep:
        return "***"
    return secret[:keep] + "***"


def _canonical_query(query: Mapping[str, Any] | None) -> str:
    """Return sorted, URL-safe ``k=v&k=v`` query string used for the signature.

    Tuya's spec: sort by ASCII order of keys; for repeated keys, keep a stable
    order.  Values are converted to ``str`` verbatim (no percent-encoding —
    Tuya's server side treats ``stringToSign`` as raw text).
    """
    if not query:
        return ""

    items: list[tuple[str, str]] = []
    for k, v in query.items():
        if v is None:
            continue
        if isinstance(v, bool):
            items.append((k, "true" if v else "false"))
        elif isinstance(v, (list, tuple)):
            for item in v:
                items.append((k, str(item)))
        else:
            items.append((k, str(v)))

    items.sort(key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in items)


def _serialize_body(body: Any) -> bytes:
    """Serialize a JSON body to bytes, using the exact representation that
    will be signed *and* sent as the HTTP payload.  Compact separators,
    ``ensure_ascii=False``.
    """
    if body is None:
        return b""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest() if data else _EMPTY_BODY_SHA256


def _hmac_sha256_upper(secret: str, message: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest().upper()
