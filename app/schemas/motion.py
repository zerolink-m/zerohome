from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.base import NonNullablePatch


class MotionEventResponse(BaseModel):
    """Motion detection event response."""

    id: int
    cam_id: int
    detected_at: int
    object_type: str
    confidence: float
    object_size: float
    license_plate: str | None
    photo_filename: str
    bbox_x: int | None
    bbox_y: int | None
    bbox_width: int | None
    bbox_height: int | None
    notified_user_ids: str | None

    class Config:
        from_attributes = True


class MotionEventListResponse(BaseModel):
    """List of motion events with pagination."""

    events: list[MotionEventResponse]
    total: int
    limit: int
    offset: int


class ONVIFSettingsUpdate(NonNullablePatch):
    """Update ONVIF settings for a camera."""

    non_nullable = ("onvif_enabled", "onvif_snapshot_enabled", "onvif_ptz_enabled")

    onvif_enabled: int | None = Field(None, ge=0, le=1)
    onvif_snapshot_enabled: int | None = Field(None, ge=0, le=1)
    onvif_ptz_enabled: int | None = Field(None, ge=0, le=1)
