from __future__ import annotations

from sqlalchemy import BigInteger, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Invitation(Base):
    """Polymorphic invitation: (device_type, device_id) points at either a cam
    or a switch (no FK — polymorphic across both tables). One invite per
    (device_type, device_id, email).

    App-side attributes keep the generic added_at/updated_at naming, mapped onto
    the real DB columns: added_at → invited_at (when the invite was sent),
    updated_at → responded_at (when the invitee responded; NULL until then)."""

    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_type: Mapped[str] = mapped_column(
        Enum("cams", "switches", name="device_type"), nullable=False
    )
    device_id: Mapped[int] = mapped_column(Integer, nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    invited_by: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Enum("pending", "accepted", name="invitation_status"),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    added_at: Mapped[int] = mapped_column("invited_at", BigInteger, nullable=False)
    updated_at: Mapped[int | None] = mapped_column("responded_at", BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "device_type", "device_id", "email", name="uq_invitations_device_email"
        ),
    )
