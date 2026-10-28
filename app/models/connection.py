from __future__ import annotations

from sqlalchemy import BigInteger, Enum, ForeignKey, Integer, JSON, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Connection(Base):
    """A per-user credential set for a third-party cloud (currently Tuya).

    The `connection_sync` worker walks every enabled Connection and materialises
    its devices as `Switch` rows (added_by='auto'). `meta` holds provider creds
    (access_id/access_secret/endpoint/tuya_uid for Tuya). `last_sync_error` is
    NULL on success, holds the last error message otherwise.
    """

    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(
        Enum("tuya_cloud", name="connection_provider"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default="1")
    last_synced_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
