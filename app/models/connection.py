from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Integer, JSON, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
        String(64), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default="1")
    last_synced_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown", server_default="unknown")
    last_checked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sync_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    credentials: Mapped[ConnectionCredentials | None] = relationship(lazy="selectin", cascade="all, delete-orphan", uselist=False)


class ConnectionCredentials(Base):
    __tablename__ = "connection_credentials"
    connection_id: Mapped[int] = mapped_column(Integer, ForeignKey("connections.id", ondelete="CASCADE"), primary_key=True)
    # Ufanet credentials intentionally remain unencrypted JSON in this table.
    credentials: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ConnectionResource(Base):
    __tablename__ = "connection_resources"
    __table_args__ = (UniqueConstraint("connection_id", "resource_type", "external_id", name="uq_connection_resource"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(Integer, ForeignKey("connections.id", ondelete="CASCADE"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    capabilities: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    available: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default="1")
    first_seen_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
