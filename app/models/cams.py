from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Integer, JSON, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Cam(Base):
    """Attribute names keep the historical app-side naming (ownuser/proto/addr/...),
    but each is explicitly mapped onto the real DB column name (owner/protocol/
    address/...) — see the first positional arg of mapped_column."""

    __tablename__ = "cams"
    __table_args__ = (UniqueConstraint("connection_resource_id", name="uq_cams_connection_resource"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_resource_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("connection_resources.id", ondelete="CASCADE"), nullable=True
    )
    ownuser: Mapped[int] = mapped_column(
        "owner", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    enabled: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    notify: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    proto: Mapped[str | None] = mapped_column("protocol", String(255), nullable=True)
    addr: Mapped[str | None] = mapped_column("address", String(255), nullable=True)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    control_proto: Mapped[str | None] = mapped_column("control_protocol", String(255), nullable=True)
    control_addr: Mapped[str | None] = mapped_column("control_address", String(255), nullable=True)
    control_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    control_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    control_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    record: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    record_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    record_meta: Mapped[dict | None] = mapped_column("record_metadata", JSON, nullable=True)
    onvif_enabled: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    onvif_snapshot_enabled: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    onvif_ptz_enabled: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
