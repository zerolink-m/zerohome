"""Tuya IoT Cloud: models (split from legacy tuya_client.py)."""

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
# DTOs (pydantic v2)
# ---------------------------------------------------------------------------


class _TuyaModel(BaseModel):
    """Base model — ignore unknown fields (Tuya adds new fields over time)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Device(_TuyaModel):
    """Device summary returned by ``/v1.0/devices/{id}`` and related endpoints."""

    id: str
    name: str | None = None
    category: str | None = None
    product_id: str | None = None
    product_name: str | None = None
    online: bool = False
    sub: bool = False
    uid: str | None = None
    uuid: str | None = None
    owner_id: str | None = None
    local_key: str | None = None
    ip: str | None = None
    model: str | None = None
    icon: str | None = None
    node_id: str | None = None
    biz_type: int | None = None
    active_time: int | None = None
    create_time: int | None = None
    update_time: int | None = None
    time_zone: str | None = None


class DPValue(_TuyaModel):
    """Single DP (data-point) value: ``{code, value}``."""

    code: str
    value: Any = None


class FunctionValues(_TuyaModel):
    """Free-form ``values`` blob attached to a function/status spec.

    Tuya returns ``values`` as a JSON-string on the wire; we deserialize it
    into a Python object where possible and expose the raw string too.
    """

    raw: str | None = None
    parsed: dict[str, Any] | None = None


class Function(_TuyaModel):
    """Function / DP definition from the specifications endpoint."""

    code: str
    name: str | None = None
    desc: str | None = None
    type: str | None = None
    values: FunctionValues | None = None


class Specifications(_TuyaModel):
    """Response of ``/v1.0/devices/{id}/specifications``."""

    category: str | None = None
    functions: list[Function] = Field(default_factory=list)
    status: list[Function] = Field(default_factory=list)


class HomeInfo(_TuyaModel):
    """A single home entry returned by ``/v1.0/users/{uid}/homes``."""

    home_id: int
    name: str | None = None
    geo_name: str | None = None
    lon: float | None = None
    lat: float | None = None
    admin: bool | None = None
    role: int | None = None


class LogEntry(_TuyaModel):
    """One record of device event log."""

    code: str | None = None
    value: str | None = None
    event_time: int | None = None
    event_id: int | None = None
    event_from: str | None = None


class WebRTCConfig(_TuyaModel):
    """Response of ``/v1.0/devices/{id}/webrtc-configs`` (camera)."""

    supports_webrtc: bool | None = None
    auth: str | None = None
    id: str | None = None
    moto_id: str | None = None
    p2p_config: dict[str, Any] | None = None
    skill: str | None = None
    vedio_clarity: Any = None
    audio_attributes: dict[str, Any] | None = None


class StreamURL(_TuyaModel):
    """Response of ``/v1.0/devices/{id}/stream/actions/allocate``."""

    url: str
