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

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        # These fields use None as the Python default so omission is allowed;
        # the validator below rejects an explicitly supplied JSON null. Reflect
        # that distinction in the generated request schema too.
        schema = handler(core_schema)
        for name in cls.non_nullable:
            prop = schema.get("properties", {}).get(name)
            if not prop:
                continue
            choices = prop.get("anyOf")
            if choices:
                choices = [choice for choice in choices if choice != {"type": "null"}]
                if len(choices) == 1:
                    prop.pop("anyOf")
                    prop.update(choices[0])
                else:
                    prop["anyOf"] = choices
            if prop.get("default", ...) is None:
                prop.pop("default")
        return schema

    @field_validator("*", mode="after")
    @classmethod
    def _reject_null(cls, v, info):
        if v is None and info.field_name in cls.non_nullable:
            raise ValueError("null is not allowed for this field")
        return v
