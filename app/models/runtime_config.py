"""Normalized, database-backed application configuration."""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SystemSettings(Base):
    """Singleton configuration revision used to identify administrative edits."""

    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class GlobalSetting(Base):
    """One shared feature/integration setting per row."""

    __tablename__ = "global_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[object] = mapped_column(JSON, nullable=False)


class NodeSetting(Base):
    """Machine-specific paths, addresses, and resource limits."""

    __tablename__ = "node_settings"

    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.node_id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[object] = mapped_column(JSON, nullable=False)


class NodeCapability(Base):
    """Capability support, administrative enablement, and optional capacity."""

    __tablename__ = "node_capabilities"

    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.node_id", ondelete="CASCADE"), primary_key=True)
    capability: Mapped[str] = mapped_column(String(64), primary_key=True)
    supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)


# Compatibility import for the focused pre-normalization test and callers from
# the in-progress implementation. It maps to the normalized global table.
GlobalConfig = GlobalSetting
