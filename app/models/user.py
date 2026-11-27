from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Enum, Integer, JSON, SmallInteger, String, false
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    """Attribute names keep the historical app-side naming (allowrecord/allowtuyalocal/
    pushmeta), but each is explicitly mapped onto the real DB column name
    (allow_record/allow_tuya_local/push_metadata)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    surname: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        Enum("admin", "user", name="user_role"),
        nullable=False,
        default="user",
        server_default="user",
    )
    allowrecord: Mapped[int | None] = mapped_column(
        "allow_record", SmallInteger, nullable=True
    )
    allowtuyalocal: Mapped[int | None] = mapped_column(
        "allow_tuya_local", SmallInteger, nullable=True
    )
    local_connections_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    push: Mapped[str | None] = mapped_column(Enum("telegram", name="user_push"), nullable=True)
    pushmeta: Mapped[dict | None] = mapped_column("push_metadata", JSON, nullable=True)
    motion_detection_allowed: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
