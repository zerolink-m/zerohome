"""Tuya IoT Cloud: constants (split from legacy tuya_client.py)."""

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
# Constants
# ---------------------------------------------------------------------------

#: Region → openapi host mapping (host only, no scheme).
TUYA_ENDPOINTS: dict[str, str] = {
    "cn": "openapi.tuyacn.com",
    "us": "openapi.tuyaus.com",
    "us-e": "openapi-ueaz.tuyaus.com",
    "eu": "openapi.tuyaeu.com",
    "eu-w": "openapi-weaz.tuyaeu.com",
    "in": "openapi.tuyain.com",
    "sg": "openapi-sg.iotbing.com",
}

#: SHA-256 of the empty string, used for empty request bodies in signature
#: string-to-sign.  Tuya documents this exact constant.
_EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

#: Default per-request timeout (seconds).
_DEFAULT_TIMEOUT = 15.0

#: Refresh window: renew the access token this many seconds before expiry.
_TOKEN_REFRESH_LEEWAY_S = 60.0

#: Business-error codes triggering exponential backoff retry.
_RETRYABLE_CODES: frozenset[int] = frozenset({1110, 1111, 1199})

#: Business-error codes that mean "token no longer valid" — refresh & retry once.
_TOKEN_INVALID_CODES: frozenset[int] = frozenset({1010, 1011, 1400})

#: Maximum retry attempts for retryable errors (including the initial call).
_MAX_RETRIES = 3

#: Backoff schedule (seconds) for wait_confirm polling of DP echoes.
_CONFIRM_BACKOFF_MS: tuple[int, ...] = (300, 500, 1000, 1500, 1500)

_LOGGER = logging.getLogger("tuya")
