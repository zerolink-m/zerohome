from __future__ import annotations

from app.schemas.base import StrictModel


class ConfigOut(StrictModel):
    version: str
    actual_version: str | None = None
