from __future__ import annotations

from sqlalchemy import BigInteger, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Functions a node can own. resource_id is 0 for global functions.
FUNCTIONS = (
    "ingest",
    "motion",
    "healthcheck",
    "tuya_events",
    "connection_sync",
    "switch_sync",
    "notify",
    "motion_cleanup",
    "session_cleanup",
)


class Node(Base):
    """A running application instance (heartbeat + what it is allowed to do)."""

    __tablename__ = "nodes"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    heartbeat_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    capabilities: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    configuration: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class Assignment(Base):
    """Lease: which node is currently responsible for (function, resource)."""

    __tablename__ = "assignments"
    __table_args__ = (UniqueConstraint("function", "resource_id", name="uq_assignments_function_resource"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    function: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    owner_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Random id of the process that holds a live lease. Same NODE_ID in another
    # process does not match, so it cannot renew or adopt the row until it expires.
    holder: Mapped[str | None] = mapped_column(String(64), nullable=True)
