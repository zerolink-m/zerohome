from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from app.schemas.base import NonNullablePatch, StrictModel


class TuyaLocalMeta(StrictModel):
    device_id: str = Field(min_length=1, max_length=64)
    ip: str = Field(min_length=1, max_length=255)
    local_key: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=8)


class TuyaCloudMeta(StrictModel):
    """Control-meta for a tuya_cloud switch row.

    Written by two paths, which must both validate here:
    * connection_sync master row → carries `dp_codes: [switch_1, ...]`
      (batch of every channel).
    * connection_sync child row  → carries a single `dp_code: switch_N`.
    * user-created single switch → may carry neither; `switch_client`'s
      `_tuya_cloud_dp_codes` falls back to `["switch_1"]`.

    `endpoint` is the bare OpenAPI host (same value stored on the parent
    Connection). `region` is kept as an accepted-but-optional alias for
    backwards compatibility with any older rows.
    """

    access_id: str = Field(min_length=1, max_length=64)
    access_secret: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(min_length=1, max_length=255)
    device_id: str = Field(min_length=1, max_length=64)
    dp_codes: list[str] | None = Field(default=None, max_length=64)
    dp_code: str | None = Field(default=None, min_length=1, max_length=64)
    region: str | None = Field(default=None, min_length=1, max_length=8)

    @model_validator(mode="after")
    def _validate(self):
        if self.dp_codes is not None and self.dp_code is not None:
            raise ValueError("controlmeta: dp_codes и dp_code взаимоисключающи")
        if self.dp_codes is not None:
            if not self.dp_codes:
                raise ValueError("controlmeta.dp_codes не может быть пустым")
            for c in self.dp_codes:
                if not isinstance(c, str) or not c:
                    raise ValueError("controlmeta.dp_codes содержит пустой код")
        return self


class CustomRequest(StrictModel):
    url: str = Field(min_length=1, max_length=2048)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    body: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _validate(self):
        if not (self.url.startswith("http://") or self.url.startswith("https://")):
            raise ValueError("url должен начинаться с http:// или https://")
        return self


class CustomResponse(StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    map: dict[str, Literal[0, 1]] = Field(max_length=64)

    @model_validator(mode="after")
    def _validate(self):
        if not (
            self.path == "status://"
            or self.path.startswith("header://")
            or self.path.startswith("json://body")
        ):
            raise ValueError(
                "path должен быть 'status://', 'header://<name>' или 'json://body...'"
            )
        return self


class CustomAction(StrictModel):
    request: CustomRequest
    response: CustomResponse | None = None


class CustomSyncAction(StrictModel):
    request: CustomRequest
    response: CustomResponse


class CustomControlMeta(StrictModel):
    on: CustomAction
    off: CustomAction
    sync: CustomSyncAction | None = None


# control_proto -> strict meta schema. _parse_controlmeta validates the raw dict
# against the matching model and returns the sanitized dump (extra keys are
# rejected by StrictModel's extra="forbid").
_CONTROLMETA_MODELS: dict[str, type[StrictModel]] = {
    "tuya_local": TuyaLocalMeta,
    "tuya_cloud": TuyaCloudMeta,
    "custom": CustomControlMeta,
}


def _parse_controlmeta(control_proto: str, controlmeta: dict) -> dict:
    """Validates controlmeta against its protocol's strict schema and returns the
    sanitized dict — callers must persist this, not the raw input, so that fields
    outside the schema (rejected by StrictModel's extra="forbid") never reach storage."""
    model = _CONTROLMETA_MODELS.get(control_proto)
    if model is None:
        raise ValueError(f"Неизвестный control_proto: {control_proto}")
    return model(**controlmeta).model_dump()


class SwitchCreate(StrictModel):
    name: str | None = Field(default=None, max_length=255)
    enabled: Literal[0, 1] | None = None
    notify: Literal[0, 1] | None = None
    control_protocol: Literal["tuya_local", "tuya_cloud", "custom"]
    control_metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self):
        self.control_metadata = _parse_controlmeta(self.control_protocol, self.control_metadata)
        return self


class SwitchPatch(NonNullablePatch, StrictModel):
    non_nullable = ("name", "enabled", "notify")

    name: str | None = Field(default=None, max_length=255)
    enabled: Literal[0, 1] | None = None
    notify: Literal[0, 1] | None = None
    control_protocol: Literal["tuya_local", "tuya_cloud", "custom"] | None = None
    control_metadata: dict | None = None

    @model_validator(mode="after")
    def _validate(self):
        if self.control_metadata is not None:
            if self.control_protocol is None:
                raise ValueError("control_metadata задан без control_protocol")
            self.control_metadata = _parse_controlmeta(self.control_protocol, self.control_metadata)
        return self


class SwitchStateIn(StrictModel):
    state: Literal[0, 1]


class SwitchStateOut(StrictModel):
    state: int
    state_updated_at: int


class SwitchOutFull(StrictModel):
    id: int
    owner: int
    name: str
    enabled: int
    notify: int
    control_protocol: str | None
    control_metadata: dict | None
    state: int
    state_updated_at: int
    updated_at: int
    added_at: int
    children: list[SwitchOutFull] | None = None


class SwitchOutRestricted(StrictModel):
    id: int
    owner: int
    name: str
    enabled: int
    control_protocol: str | None
    state: int
    updated_at: int
    added_at: int
    children: list[SwitchOutRestricted] | None = None
