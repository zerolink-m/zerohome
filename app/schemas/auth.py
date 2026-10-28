from __future__ import annotations

from pydantic import EmailStr, Field, field_validator

from app.schemas.base import StrictModel


class LoginRequest(StrictModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=1, max_length=128)

    @field_validator('email')
    @classmethod
    def _lower_email(cls, v: str) -> str:
        return v.lower()


class RegisterRequest(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    surname: str = Field(min_length=1, max_length=64)
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=1, max_length=128)

    @field_validator('email')
    @classmethod
    def _lower_email(cls, v: str) -> str:
        return v.lower()


class TokenOut(StrictModel):
    token: str


class IdOut(StrictModel):
    id: int
