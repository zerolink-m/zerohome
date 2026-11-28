from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field, RootModel

from app.schemas.base import StrictModel


class SessionOut(StrictModel):
    id: int
    uid: int
    ua: str
    ip: str
    last_login: int
    last_seen: int
    updated_at: int
    added_at: int


class SessionParentPath(StrictModel):
    user_id: int = Field(description="Идентификатор пользователя")


class SessionIdPath(SessionParentPath):
    id: int = Field(description="Идентификатор сессии")


class SessionIdsPath(SessionParentPath):
    ids: str = Field(description="Идентификаторы сессий через запятую")


class SessionBulkDeleteQuery(StrictModel):
    partial_allowed: str | None = Field(
        default=None,
        description="Частичное удаление. options.partial_allowed в теле имеет приоритет.",
    )


class SessionListQuery(StrictModel):
    limit: int = Field(default=25, ge=1, le=255)
    offset: int = Field(default=0, ge=0)
    order: str = Field(default="asc", description="asc/desc; можно указать через запятую")
    sort_by: str | None = None
    search: str | None = None
    search_column: str | None = None
    search_method: str | None = Field(default=None, description="По умолчанию include")


class SessionListSettings(StrictModel):
    total: int
    limit: int
    offset: int
    order: str
    sort_by: str | None
    search: str | None
    search_column: str | None
    search_method: str | None
    filter: str | None


class SessionsListData(StrictModel):
    settings: SessionListSettings
    sessions: list[SessionOut]


class SessionDeleted(StrictModel):
    id: int


class SessionsBulkData(StrictModel):
    sessions: list[SessionOut]


class SessionsDeletedData(StrictModel):
    sessions: list[SessionDeleted]


class SessionDeleteFailure(StrictModel):
    id: int
    error: dict[str, Any]


class SessionsDeleteResult(StrictModel):
    succeeded: list[SessionDeleted]
    failed: list[SessionDeleteFailure]


class SessionsDeletePartialData(StrictModel):
    sessions: SessionsDeleteResult


class EmptySessionData(StrictModel):
    pass


class SessionsDeleteErrorData(RootModel[SessionsDeletePartialData | EmptySessionData]):
    pass


class SessionsDeleteRequestData(StrictModel):
    """DELETE ignores data fields. The options object controls partial deletion."""

    model_config = ConfigDict(extra="allow")
