from __future__ import annotations

from sqlalchemy import BigInteger, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Sharing(Base):
    """Polymorphic time-limited share link. (device_type, device_id) points at a
    cam or switch. `type` is 'record' or 'stream'; `filename` is set only for
    record shares (the specific recording)."""

    __tablename__ = "sharings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(
        Enum("record", "stream", "device_use", name="sharing_type"), nullable=False
    )
    device_type: Mapped[str] = mapped_column(
        Enum("cams", "switches", name="device_type"), nullable=False
    )
    device_id: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
