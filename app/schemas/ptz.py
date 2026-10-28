from __future__ import annotations

from pydantic import Field, model_validator

from app.schemas.base import StrictModel


class PTZCommandIn(StrictModel):
    pan: float | None = Field(default=None, allow_inf_nan=False)
    tilt: float | None = Field(default=None, allow_inf_nan=False)
    zoom: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def at_least_one_field(self) -> "PTZCommandIn":
        if self.pan is None and self.tilt is None and self.zoom is None:
            raise ValueError("Хотя бы одно из полей pan/tilt/zoom должно быть задано")
        return self


class PTZPositionOut(StrictModel):
    pan: float
    tilt: float
    zoom: float
