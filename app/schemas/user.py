from __future__ import annotations

from typing import Literal

from pydantic import EmailStr, Field, StrictBool, field_validator, model_validator

from app.schemas.base import NonNullablePatch, StrictModel


class TelegramPushMeta(StrictModel):
    bot_token: str = Field(min_length=1, max_length=255)
    telegram_id: int = Field(ge=1, le=9223372036854775807)


class PushMeta(StrictModel):
    telegram: TelegramPushMeta | None = None


class UserCreate(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    surname: str = Field(min_length=1, max_length=64)
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=1, max_length=128)
    role: Literal["admin", "user"] | None = None
    local_connections_allowed: StrictBool = False

    @field_validator('email')
    @classmethod
    def _lower_email(cls, v: str) -> str:
        return v.lower()


class UserOut(StrictModel):
    id: int
    name: str
    surname: str
    email: str
    role: Literal["admin", "user"]
    allow_record: int
    allow_tuya_local: int
    local_connections_allowed: bool
    motion_detection_allowed: int
    push: Literal["telegram"] | None
    push_metadata: PushMeta | None
    added_at: int
    updated_at: int


class UserPatch(NonNullablePatch, StrictModel):
    non_nullable = (
        "name", "surname", "email", "password", "role", "motion_detection_allowed", "local_connections_allowed",
    )

    name: str | None = Field(default=None, min_length=1, max_length=64)
    surname: str | None = Field(default=None, min_length=1, max_length=64)
    email: EmailStr | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=128)
    role: Literal["admin", "user"] | None = None
    allow_record: Literal[0, 1] | None = None
    allow_tuya_local: Literal[0, 1] | None = None
    local_connections_allowed: StrictBool | None = None
    motion_detection_allowed: Literal[0, 1] | None = None
    push: Literal["telegram"] | None = None
    push_metadata: PushMeta | None = None

    @field_validator('email')
    @classmethod
    def _lower_email(cls, v: str | None) -> str | None:
        if v is not None:
            return v.lower()
        return v

    @model_validator(mode="after")
    def _validate_push(self):
        if self.push == "telegram" and (self.push_metadata is None or self.push_metadata.telegram is None):
            raise ValueError("Для push=telegram требуется push_metadata.telegram")
        if self.push is None and self.push_metadata is not None:
            raise ValueError("push_metadata задан без push")
        return self
