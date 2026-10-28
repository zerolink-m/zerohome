from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Switch(Base):
    """A controllable switch/socket.

    Rows come from two sources (`added_by`):
      - 'user': manually created; connection_id/external_id are NULL.
      - 'auto': materialised by the connection sync worker from a cloud
        provider (Tuya). Such rows always carry connection_id + external_id.

    Multichannel model (one physical device → many rows):
      - is_master=1: master row for the whole physical device (parent NULL,
        external_switch_channel NULL).
      - is_master=0: a single channel (parent = master.id,
        external_switch_channel = DP code like "switch_1").

    The CHECK constraint `ck_switches_added_by_consistency` enforces these
    invariants at the DB level; the UNIQUE
    (connection_id, external_id, external_switch_channel) lets a master
    (channel NULL) and each child share one device_id.

    App-side attribute names keep the historical naming (ownuser/control_proto/
    controlmeta/is_master), but each is explicitly mapped onto the real DB
    column name (owner/control_protocol/control_metadata/is_main).
    """

    __tablename__ = "switches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ownuser: Mapped[int] = mapped_column(
        "owner", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    enabled: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    notify: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    control_proto: Mapped[str | None] = mapped_column("control_protocol", String(255), nullable=True)
    controlmeta: Mapped[dict | None] = mapped_column("control_metadata", JSON, nullable=True)
    state: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    state_updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_by: Mapped[str] = mapped_column(
        Enum("user", "auto", name="switch_added_by"),
        nullable=False,
        default="user",
        server_default="user",
    )
    connection_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("connections.id", ondelete="CASCADE"), nullable=True
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_master: Mapped[int] = mapped_column("is_main", SmallInteger, nullable=False, default=0, server_default="0")
    parent: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("switches.id", ondelete="CASCADE"), nullable=True, index=True
    )
    external_switch_channel: Mapped[str | None] = mapped_column(String(128), nullable=True)
    added_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "external_id",
            "external_switch_channel",
            name="uq_switches_connection_external_channel",
        ),
        CheckConstraint(
            "("
            "  added_by = 'user' AND connection_id IS NULL AND external_id IS NULL "
            "  AND external_switch_channel IS NULL AND is_main = 0 AND parent IS NULL"
            ") OR ("
            "  added_by = 'auto' AND connection_id IS NOT NULL AND external_id IS NOT NULL "
            "  AND ("
            "    (is_main = 1 AND parent IS NULL AND external_switch_channel IS NULL)"
            "    OR"
            "    (is_main = 0 AND parent IS NOT NULL AND external_switch_channel IS NOT NULL)"
            "  )"
            ")",
            name="ck_switches_added_by_consistency",
        ),
    )
