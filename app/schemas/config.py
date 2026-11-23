from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.base import StrictModel


class ConfigOut(StrictModel):
    version: str
    actual_version: str | None = None


class ServerTimeOut(StrictModel):
    time: int


class SettingOut(StrictModel):
    key: str
    value: Any


class SettingPatch(StrictModel):
    value: Any


class NodeCapabilityOut(StrictModel):
    supported: bool
    enabled: bool
    capacity: int | None = None


class NodeConfigurationOut(StrictModel):
    node_id: str
    address: str | None = None
    started_at: int
    heartbeat_at: int
    version: str
    settings: dict[str, Any]
    capabilities: dict[str, NodeCapabilityOut]


class AssignmentOut(StrictModel):
    function: str
    resource_id: int
    owner_node_id: str | None = None
    generation: int
    expires_at: int
    updated_at: int


class NodeConfigurationPatch(StrictModel):
    settings: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)
