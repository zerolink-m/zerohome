"""Tuya IoT Cloud: errors (split from legacy tuya_client.py)."""

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



# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TuyaError(Exception):
    """Base class for all Tuya SDK errors.

    Attributes:
        code: Tuya numeric error code (``0`` if not from the API).
        msg:  Human-readable Tuya error message.
        tid:  Trace ID (``t`` field) from the API response, if available.
    """

    def __init__(self, msg: str, code: int = 0, tid: str | None = None) -> None:
        super().__init__(f"[{code}] {msg}" + (f" (tid={tid})" if tid else ""))
        self.code = code
        self.msg = msg
        self.tid = tid


class TuyaAuthError(TuyaError):
    """Authentication / signature / token-related error (1001, 1004, 1005, 1010, 1011, 1013, 1114)."""


class TuyaPermissionError(TuyaError):
    """Permission denied (1106)."""


class TuyaRateLimitError(TuyaError):
    """Rate limited / concurrency / quota (1110, 1111, 1199, 28841104)."""


class TuyaDeviceOfflineError(TuyaError):
    """Device is offline (2001, 28841002)."""


class TuyaDeviceNotFoundError(TuyaError):
    """Device / product not found (2010, 2007)."""


class TuyaValueError(TuyaError):
    """Illegal / unsupported parameter or DP value (2003, 2008, 1109)."""


class TuyaAPIError(TuyaError):
    """Generic API error — everything not covered by more specific classes."""


_ERROR_CODE_MAP: dict[int, type[TuyaError]] = {
    1001: TuyaAuthError,
    1004: TuyaAuthError,
    1005: TuyaAuthError,
    1010: TuyaAuthError,
    1011: TuyaAuthError,
    1013: TuyaAuthError,
    1114: TuyaAuthError,
    1400: TuyaAuthError,
    1106: TuyaPermissionError,
    1110: TuyaRateLimitError,
    1111: TuyaRateLimitError,
    1199: TuyaRateLimitError,
    28841104: TuyaRateLimitError,
    2001: TuyaDeviceOfflineError,
    28841002: TuyaDeviceOfflineError,
    2010: TuyaDeviceNotFoundError,
    2007: TuyaDeviceNotFoundError,
    2003: TuyaValueError,
    2008: TuyaValueError,
    1109: TuyaValueError,
}


def _exception_for(code: int, msg: str, tid: str | None) -> TuyaError:
    cls = _ERROR_CODE_MAP.get(code, TuyaAPIError)
    return cls(msg, code=code, tid=tid)
