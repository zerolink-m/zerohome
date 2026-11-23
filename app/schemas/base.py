from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, field_validator


class StrictModel(BaseModel):
    """Base for all request/response schemas: rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")


class NonNullablePatch(BaseModel):
    """Mixin for PATCH schemas: fields listed in `non_nullable` (columns declared
    nullable=False in app/models) accept a value or omission, but never an explicit
    null — that gives a validation error (269, VALIDATION_ERROR) instead of a DB error."""

    non_nullable: ClassVar[tuple[str, ...]] = ()

    @field_validator("*", mode="after")
    @classmethod
    def _reject_null(cls, v, info):
        if v is None and info.field_name in cls.non_nullable:
            raise ValueError("null is not allowed for this field")
        return v
