from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.base import NonNullablePatch


class PushSettingsBase(BaseModel):
    """Base push settings schema."""

    enabled: int = Field(1, ge=0, le=1)

    # Motion Detection
    motion_detection_enabled: int = Field(0, ge=0, le=1)
    motion_min_confidence: float = Field(0.6, ge=0.0, le=1.0)
    motion_min_object_size: float = Field(0.02, ge=0.0, le=1.0)
    motion_cooldown_seconds: int = Field(0, ge=0)
    motion_detect_person: int = Field(1, ge=0, le=1)
    motion_detect_dog: int = Field(1, ge=0, le=1)
    motion_detect_cat: int = Field(1, ge=0, le=1)
    motion_detect_car: int = Field(1, ge=0, le=1)
    motion_license_plate_ocr: int = Field(1, ge=0, le=1)

    # Other notifications
    notify_offline: int = Field(1, ge=0, le=1)
    notify_online: int = Field(1, ge=0, le=1)
    notify_state_change: int = Field(1, ge=0, le=1)


class PushSettingsUpdate(NonNullablePatch):
    """Update push settings (all fields optional)."""

    model_config = ConfigDict(extra="forbid")

    non_nullable = (
        "enabled", "motion_detection_enabled", "motion_min_confidence", "motion_min_object_size",
        "motion_cooldown_seconds", "motion_detect_person", "motion_detect_dog", "motion_detect_cat",
        "motion_detect_car", "motion_license_plate_ocr", "notify_offline", "notify_online",
        "notify_state_change",
    )

    enabled: int | None = Field(None, ge=0, le=1)

    motion_detection_enabled: int | None = Field(None, ge=0, le=1)
    motion_min_confidence: float | None = Field(None, ge=0.0, le=1.0)
    motion_min_object_size: float | None = Field(None, ge=0.0, le=1.0)
    motion_cooldown_seconds: int | None = Field(None, ge=0)
    motion_detect_person: int | None = Field(None, ge=0, le=1)
    motion_detect_dog: int | None = Field(None, ge=0, le=1)
    motion_detect_cat: int | None = Field(None, ge=0, le=1)
    motion_detect_car: int | None = Field(None, ge=0, le=1)
    motion_license_plate_ocr: int | None = Field(None, ge=0, le=1)

    notify_offline: int | None = Field(None, ge=0, le=1)
    notify_online: int | None = Field(None, ge=0, le=1)
    notify_state_change: int | None = Field(None, ge=0, le=1)


class PushSettingsResponse(PushSettingsBase):
    """Push settings response."""

    id: int
    user_id: int
    device_type: str
    device_id: int
    created_at: int
    updated_at: int

    class Config:
        from_attributes = True
