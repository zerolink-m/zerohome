from __future__ import annotations

from sqlalchemy import BigInteger, Enum, Float, Integer, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PushSettings(Base):
    """Individual push notification settings per user per device."""

    __tablename__ = "push_settings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    device_type: Mapped[str] = mapped_column(
        Enum("cams", "switches", name="device_type_enum"), nullable=False
    )
    device_id: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default="1")

    # Motion Detection settings
    motion_detection_enabled: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    motion_min_confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.6, server_default="0.6"
    )
    motion_min_object_size: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.02, server_default="0.02"
    )
    motion_detect_person: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )
    motion_detect_dog: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )
    motion_detect_cat: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )
    motion_detect_car: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )
    motion_license_plate_ocr: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )

    # Other notification settings
    notify_offline: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )
    notify_online: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )
    notify_state_change: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default="1"
    )

    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
