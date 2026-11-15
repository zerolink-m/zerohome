from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Integer, String, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ua: Mapped[str] = mapped_column(String(255), nullable=False)
    ip: Mapped[str] = mapped_column(String(255), nullable=False)
    last_login: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
