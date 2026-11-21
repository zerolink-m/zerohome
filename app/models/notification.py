"""Notification jobs: one row per outbound push, claimed by a single node."""
from __future__ import annotations

from sqlalchemy import BigInteger, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

KINDS = ("motion", "health", "switch", "tuya", "manual")
STATES = ("pending", "sending", "sent", "failed")


class NotificationJob(Base):
    """A push to deliver. Claim is a conditional update; claimed_generation fences a stale owner."""

    __tablename__ = "notification_jobs"
    __table_args__ = (
        Index("ix_notification_jobs_state_available", "state", "available_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    device_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    photo_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
