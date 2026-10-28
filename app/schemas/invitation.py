from __future__ import annotations

from typing import Literal

from pydantic import EmailStr, Field, field_validator

from app.schemas.base import StrictModel


class InvitationCreate(StrictModel):
    device_type: Literal["cams", "switches"]
    device_id: int
    email: EmailStr = Field(max_length=255)

    @field_validator('email')
    @classmethod
    def _lower_email(cls, v: str) -> str:
        return v.lower()


class InvitationOut(StrictModel):
    id: int
    device_type: Literal["cams", "switches"]
    device_id: int
    email: str
    invited_by: int
    status: Literal["pending", "accepted"]
    added_at: int
    updated_at: int | None


class InvitationMineOut(StrictModel):
    id: int
    device_type: Literal["cams", "switches"]
    device_id: int
    device_name: str
    invited_by: int
    status: Literal["pending", "accepted"]
    added_at: int
    updated_at: int | None
