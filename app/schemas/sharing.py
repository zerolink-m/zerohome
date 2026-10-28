from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.base import StrictModel


class SharingCreate(StrictModel):
    device_type: Literal["cams", "switches"]
    device_id: int
    type: Literal["record", "stream", "device_use"]
    filename: str | None = Field(default=None, max_length=255)
    ttl_seconds: int | None = Field(default=None, ge=1, le=31536000)

    @model_validator(mode="after")
    def _validate_filename(self):
        if self.type == "record":
            if not self.filename:
                raise ValueError("Для type=record требуется filename")
        else:
            if self.filename is not None:
                raise ValueError("filename допустим только для type=record")
        return self


class SharingOut(StrictModel):
    id: int
    uid: int
    type: Literal["record", "stream", "device_use"]
    device_type: Literal["cams", "switches"]
    device_id: int
    filename: str | None
    updated_at: int
    added_at: int


class SharingCreateOut(StrictModel):
    id: int
    type: Literal["record", "stream", "device_use"]
    device_type: Literal["cams", "switches"]
    device_id: int
    filename: str | None
    token: str
    expires_at: int
