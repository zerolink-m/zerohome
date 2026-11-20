from __future__ import annotations

from sqlalchemy import BigInteger, Enum, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MotionEvent(Base):
    """Motion detection event with detected object details."""

    __tablename__ = "motion_events"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True,
    )
    cam_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    detected_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(
        Enum("person", "dog", "cat", "car", name="object_type_enum"),
        nullable=False,
        index=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    object_size: Mapped[float] = mapped_column(Float, nullable=False)
    license_plate: Mapped[str | None] = mapped_column(String(20), nullable=True)
    photo_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Object key (motion/<cam_id>/<filename>). NULL on rows written before the storage layer:
    # those are still served from photo_filename under MOTION_FILES_DIR.
    photo_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    photo_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bbox_x: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox_y: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notified_user_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
