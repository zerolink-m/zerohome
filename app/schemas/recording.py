from __future__ import annotations

from typing import Literal

from app.schemas.base import StrictModel


class RecordingOut(StrictModel):
    id: str
    filename: str
    format: Literal["mp4", "avi", "mkv"]
    started_at: int
    size_bytes: int
