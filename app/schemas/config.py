from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.base import StrictModel


class ConfigOut(StrictModel):
    version: str
    actual_version: str | None = None


class ServerTimeOut(StrictModel):
    time: int


class RuntimeGlobalPatch(StrictModel):
    settings: dict[str, Any]


class RuntimeNodePatch(StrictModel):
    settings: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)
