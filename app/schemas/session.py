from __future__ import annotations

from app.schemas.base import StrictModel


class SessionOut(StrictModel):
    id: int
    uid: int
    ua: str
    ip: str
    last_login: int
    updated_at: int
    added_at: int
