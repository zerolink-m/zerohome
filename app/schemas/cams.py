from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.base import NonNullablePatch, StrictModel


class CamCreate(StrictModel):
    name: str | None = Field(default=None, max_length=255)
    enabled: Literal[0, 1] | None = None
    notify: Literal[0, 1] | None = None
    protocol: Literal["rtsp"]
    address: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    path: str | None = Field(default=None, max_length=255)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)
    control_protocol: Literal["onvif"] | None = None
    control_address: str | None = Field(default=None, max_length=255)
    control_port: int | None = Field(default=None, ge=1, le=65535)
    control_username: str | None = Field(default=None, max_length=255)
    control_password: str | None = Field(default=None, max_length=128)


class CamPatch(NonNullablePatch, StrictModel):
    non_nullable = ("name", "enabled", "notify", "protocol", "address", "port", "path", "username", "password")

    name: str | None = Field(default=None, max_length=255)
    enabled: Literal[0, 1] | None = None
    notify: Literal[0, 1] | None = None
    protocol: Literal["rtsp"] | None = None
    address: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    path: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=128)
    control_protocol: Literal["onvif"] | None = None
    control_address: str | None = Field(default=None, max_length=255)
    control_port: int | None = Field(default=None, ge=1, le=65535)
    control_username: str | None = Field(default=None, max_length=255)
    control_password: str | None = Field(default=None, max_length=128)


class CamOutFull(StrictModel):
    id: int
    owner: int
    name: str
    enabled: int
    notify: int
    protocol: str | None
    address: str | None
    port: int
    path: str
    username: str
    password: str
    control_protocol: str | None
    control_address: str | None
    control_port: int | None
    control_username: str | None
    control_password: str | None
    onvif_enabled: int
    onvif_snapshot_enabled: int
    onvif_ptz_enabled: int
    record: int
    record_type: str | None
    record_metadata: dict | None
    updated_at: int
    added_at: int


class CamOutRestricted(StrictModel):
    id: int
    owner: int
    name: str
    enabled: int
    protocol: str | None
    address: str | None
    port: int
    path: str
    control_protocol: str | None
    onvif_enabled: int
    onvif_snapshot_enabled: int
    onvif_ptz_enabled: int
    record: int
    record_type: str | None
    updated_at: int
    added_at: int


class CamRecordingLocalMeta(StrictModel):
    path: str = Field(min_length=1, max_length=1024)


class CamRecordingMeta(StrictModel):
    local: CamRecordingLocalMeta | None = None


class CamRecordingPatch(NonNullablePatch, StrictModel):
    non_nullable = ("record",)

    record: Literal[0, 1] | None = None
    record_type: Literal["local"] | None = None
    record_metadata: CamRecordingMeta | None = None


class CamRecordingOut(StrictModel):
    record: int
    record_type: str | None
    record_metadata: dict | None


class CamOnvifPatch(NonNullablePatch, StrictModel):
    non_nullable = ("onvif_enabled", "onvif_snapshot_enabled", "onvif_ptz_enabled")

    onvif_enabled: Literal[0, 1] | None = None
    onvif_snapshot_enabled: Literal[0, 1] | None = None
    onvif_ptz_enabled: Literal[0, 1] | None = None
