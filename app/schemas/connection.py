from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from app.schemas.base import NonNullablePatch, StrictModel


# Canonical Tuya region → OpenAPI host. Keys are region codes, values are
# fully-qualified hostnames (no scheme).
#
# NOTE (recovery 2026-08-08): the original module had a `TUYA_ENDPOINTS`
# module-level dict; its exact key/value split (region-code vs host as key)
# could not be recovered from bytecode dumps before the process died. We
# accept BOTH forms via the validator below — an endpoint that appears as
# either a key OR a value is considered valid.
TUYA_ENDPOINTS: dict[str, str] = {
    "cn":   "openapi.tuyacn.com",
    "us":   "openapi.tuyaus.com",
    "us-e": "openapi-ueaz.tuyaus.com",
    "eu":   "openapi.tuyaeu.com",
    "eu-w": "openapi-weaz.tuyaeu.com",
    "in":   "openapi.tuyain.com",
    "sg":   "openapi-sg.iotbing.com",
}


def _validate_tuya_meta(meta: dict) -> None:
    for key in ("access_id", "access_secret", "endpoint"):
        v = meta.get(key)
        if not isinstance(v, str) or not v:
            raise ValueError(f"meta.{key} обязательно и должно быть непустой строкой")
    endpoint = meta["endpoint"]
    if endpoint not in TUYA_ENDPOINTS and endpoint not in TUYA_ENDPOINTS.values():
        allowed = sorted(set(TUYA_ENDPOINTS.keys()) | set(TUYA_ENDPOINTS.values()))
        raise ValueError("meta.endpoint должен быть одним из: " + ", ".join(allowed))
    allowed_keys = {"access_id", "access_secret", "endpoint", "tuya_uid"}
    extra = set(meta.keys()) - allowed_keys
    if extra:
        raise ValueError("meta содержит неизвестные ключи: " + ", ".join(sorted(extra)))


def redact_meta(provider: str, meta: dict) -> dict:
    """Strips write-only credential fields before returning meta to the client.
    Keeps public identifiers (access_id, endpoint) so the client can display
    "connected to project X"; hides secrets so a screenshot doesn't leak them."""
    if provider == "tuya_cloud":
        return {
            "access_id": meta.get("access_id"),
            "endpoint": meta.get("endpoint"),
            "tuya_uid": meta.get("tuya_uid"),
        }
    return {}


class ConnectionCreate(StrictModel):
    provider: Literal["tuya_cloud"]
    name: str = Field(min_length=1, max_length=255)
    meta: dict[str, Any]
    enabled: Literal[0, 1] = 1

    @model_validator(mode="after")
    def _validate_meta(self):
        if self.provider == "tuya_cloud":
            _validate_tuya_meta(self.meta)
        return self


class ConnectionPatch(NonNullablePatch, StrictModel):
    non_nullable = ("name", "enabled", "meta")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: Literal[0, 1] | None = None
    meta: dict[str, Any] | None = None


class ConnectionOut(StrictModel):
    id: int
    uid: int
    provider: str
    name: str
    meta: dict[str, Any]
    enabled: int
    last_synced_at: int | None
    last_sync_error: str | None
    added_at: int
    updated_at: int
