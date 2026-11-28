from __future__ import annotations

import time

from cursedapi.auth import Policy
from cursedapi.filtering import FilterField
from cursedapi.sqlalchemy import SQLAlchemyBackend
from fastapi import APIRouter, Depends, Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import codes
from app.core.codes import FORBIDDEN_RESOURCE, OBJECT_NOT_FOUND
from app.core.cursed_api import ResponseSpec, declare_generated_route, describe_contract, describe_generated_route
from app.core.errors import AppError
from app.core.introspection import supports
from app.core.response import bulk, bulk_listing, deleted, failed_item, single
from app.core.validation import field_error
from app.core.rate_limit import rate_limit
from app.core.security import TokenPayload
from app.db.session import get_db
from app.deps.auth import IDLE_SECONDS, get_bearer_token, require_access
from app.deps.request_body import read_envelope
from app.models.session import Session as SessionModel
from app.models.user import User
from app.deps.ids import parse_ids
from app.routers.users import MAX_BULK
from app.schemas.session import (
    SessionBulkDeleteQuery,
    SessionDeleted,
    SessionIdPath,
    SessionIdsPath,
    SessionListQuery,
    SessionOut,
    SessionParentPath,
    SessionsBulkData,
    SessionsDeleteErrorData,
    SessionsDeletePartialData,
    SessionsDeletedData,
    SessionsDeleteRequestData,
    SessionsListData,
)

router = APIRouter(prefix="/v1/users", tags=["sessions"])

SESSIONS_COLUMNS = ["id", "uid", "ua", "ip", "last_login", "last_seen", "added_at", "updated_at"]
_SESSION_LIST_FILTERS = {
    name: FilterField(int, {"==", "!=", "<", "<=", ">", ">=", "IN", "NOT IN", "BETWEEN", "NOT BETWEEN"})
    for name in ("id", "uid", "last_login", "last_seen", "added_at", "updated_at")
}
_USER_ID = TypeAdapter(int)
_READ_BUDGET = rate_limit("users_read", capacity=60, per_minute=60)
_LIST_PATH = "/v1/users/{user_id}/sessions"

_ACCESS_RESPONSES = {
    "token_missing": ResponseSpec(codes.TOKEN_MISSING, "Требуется токен сессии"),
    "token_invalid": ResponseSpec(codes.TOKEN_ERROR, "Токен недействителен или отозван"),
    "token_expired": ResponseSpec(codes.TOKEN_EXPIRED, "Сессия просрочена"),
    "forbidden_user": ResponseSpec(codes.FORBIDDEN_RESOURCE, "Доступ к чужим сессиям запрещён"),
    "rate_limited": ResponseSpec(codes.RATE_LIMITED, "Превышен общий лимит запросов"),
    "operation_rate_limited": ResponseSpec(codes.ENDPOINT_RATE_LIMITED, "Превышен лимит операции"),
}
_DELETE_ERRORS = {
    "invalid_query": ResponseSpec(codes.INVALID_QUERY, "Неверное значение partial_allowed"),
    "unsupported_body": ResponseSpec(codes.UNSUPPORTED_BODY_FORMAT, "Тело должно быть JSON"),
    "invalid_json": ResponseSpec(codes.INVALID_JSON, "Синтаксически неверный JSON"),
}


def _schema_access(ctx):
    if ctx.principal is None:
        return False
    try:
        user_id = _USER_ID.validate_python(ctx.path_params.get("user_id"))
    except ValidationError:
        return False
    return ctx.principal.uid == user_id or ctx.principal.role == "admin"


class SessionListPolicy(Policy):
    def authorize(self, ctx):
        # Discovery uses the same owner/admin scope without touching session rows.
        if (ctx.request.scope.get("cursed_schema_discovery") or ctx.request.method == "OPTIONS"
                or "/_cursed/schema/" in ctx.request.url.path):
            return _schema_access(ctx)
        if ctx.principal is None:
            get_bearer_token(None)
        return True

    async def scope(self, ctx):
        raw_id = ctx.path_params["user_id"]
        try:
            user_id = _USER_ID.validate_python(raw_id)
        except ValidationError as exc:
            raise AppError(codes.VALIDATION_ERROR, "Some path parameters are invalid", path={
                "user_id": field_error("must be an integer", type="int", value=raw_id),
            }) from exc
        await _check_access_and_user(user_id, ctx.principal, ctx.session)
        return SessionModel.uid == user_id


def register_session_list(api):
    sessions = api.add_resource(
        _LIST_PATH,
        singular="session", plural="sessions", read_schema=SessionOut,
        backend=SQLAlchemyBackend(SessionModel), policy=SessionListPolicy(),
        operations={"list"}, search_fields=SESSIONS_COLUMNS, sort_fields=SESSIONS_COLUMNS,
        filter_fields=_SESSION_LIST_FILTERS,
    )

    @sessions.hook("after_authenticate")
    async def charge_read_budget(ctx):
        await _READ_BUDGET(ctx.request)

    describe_generated_route(
        api, _LIST_PATH, "GET", summary="Список сессий пользователя",
        description="Просроченные сессии удаляются перед выборкой. Доступ предоставлен владельцу или администратору.",
        path_schema=SessionParentPath, query_schema=SessionListQuery,
        responses={
            **_ACCESS_RESPONSES,
            "sessions": ResponseSpec(codes.SUCCESS, "Найдены активные сессии", schema=SessionsListData),
            "empty": ResponseSpec(codes.EMPTY_RESULT, "Активных сессий нет", schema=SessionsListData),
            "missing_user": ResponseSpec(codes.OBJECT_NOT_FOUND, "Пользователь не найден"),
            "invalid_query": ResponseSpec(codes.INVALID_QUERY, "Неверный запрос или выражение filter"),
            "invalid_sort": ResponseSpec(codes.INVALID_SORT_FIELD, "Недоступное поле сортировки"),
            "invalid_search": ResponseSpec(codes.INVALID_SEARCH_COLUMN, "Недоступное поле поиска"),
            "invalid_page": ResponseSpec(codes.INVALID_LIMIT_OFFSET, "Некорректный limit или offset"),
            "limit_exceeded": ResponseSpec(codes.LIMIT_TOO_LARGE, "Превышен предел limit"),
            "empty_search": ResponseSpec(codes.SEARCH_EMPTY_POSITION, "Пустая позиция в параметрах поиска"),
            "search_lengths": ResponseSpec(codes.SEARCH_LENGTH_MISMATCH, "Разная длина параметров поиска"),
            "expand": ResponseSpec(codes.EXPAND_NOT_IMPLEMENTED, "Связанные ресурсы не раскрываются"),
        },
    )
    declare_generated_route(
        api, _LIST_PATH, "GET", "limit", "offset", "pagination", "order", "sort_by",
        "search", "search_column", "search_method", "multi_search", "filter", max_limit=255,
        auth="bearer",
    )


def _to_out(session: SessionModel) -> dict:
    return SessionOut(
        id=session.id,
        uid=session.uid,
        ua=session.ua,
        ip=session.ip,
        last_login=session.last_login,
        last_seen=session.last_seen,
        added_at=session.added_at,
        updated_at=session.updated_at,
    ).model_dump()


async def _check_access_and_user(
    user_id: int, payload: TokenPayload, db: AsyncSession
) -> None:
    if payload.uid != user_id and payload.role != "admin":
        raise AppError(FORBIDDEN_RESOURCE, "Доступ к чужим сессиям запрещён")
    result = await db.execute(delete(SessionModel).where(
        SessionModel.uid == user_id, SessionModel.last_seen <= int(time.time()) - IDLE_SECONDS))
    if result.rowcount:
        await db.commit()
    user = await db.get(User, user_id)
    if user is None:
        raise AppError(OBJECT_NOT_FOUND, "Пользователь не найден", path={
            "user_id": field_error("object not found", type="int", value=user_id)})


def _not_found(id) -> AppError:
    return AppError(OBJECT_NOT_FOUND, "Сессия не найдена", path={
        "id": field_error("object not found", type="int", value=id)})


async def _partial_request(request: Request) -> bool:
    """DELETE bulk accepts no body, or a Request object; options override query."""
    _, options = await read_envelope(request, optional=True)
    return options.partial_allowed


async def _optional_request_body(request: Request) -> None:
    """CRUD DELETE may omit its body; a supplied body must still be a Request object."""
    await read_envelope(request, optional=True)


@router.get("/{user_id}/sessions/bulk/{ids}", dependencies=[Depends(rate_limit("users_read", capacity=60, per_minute=60))])
@supports("bulk", max_bulk=MAX_BULK)
@describe_contract(
    schema_access=_schema_access,
    summary="Получить сессии по списку идентификаторов",
    description="Сессии возвращаются в порядке запрошенных ID. Повторные ID учитываются один раз.",
    path_schema=SessionIdsPath,
    responses={
        **_ACCESS_RESPONSES,
        "found": ResponseSpec(codes.SUCCESS, "Все сессии найдены", schema=SessionsBulkData),
        "missing": ResponseSpec(codes.OBJECT_NOT_FOUND, "Пользователь или сессия не найдены"),
        "invalid_ids": ResponseSpec(codes.VALIDATION_ERROR, "Некорректный пользователь или список ID"),
        "too_many": ResponseSpec(codes.BULK_LIMIT_EXCEEDED, "Слишком много идентификаторов"),
    },
)
async def get_sessions_bulk(
    user_id: int,
    ids: str,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    id_list = parse_ids(ids, MAX_BULK)
    await _check_access_and_user(user_id, payload, db)
    rows = (await db.execute(
        select(SessionModel).where(SessionModel.uid == user_id, SessionModel.id.in_(id_list)))).scalars().all()
    missing = sorted(set(id_list) - {s.id for s in rows})
    if missing:
        raise AppError(OBJECT_NOT_FOUND, "Сессии не найдены", path={
            "ids": field_error("objects not found", value=",".join(map(str, missing)))})
    by_id = {s.id: s for s in rows}
    return bulk_listing("sessions", [_to_out(by_id[i]) for i in id_list], requested=len(id_list))


@router.delete("/{user_id}/sessions/bulk/{ids}", dependencies=[Depends(rate_limit("users_write", capacity=20, per_minute=20))])
@supports("bulk", "partial", max_bulk=MAX_BULK)
@describe_contract(
    schema_access=_schema_access,
    summary="Удалить несколько сессий",
    description="По умолчанию удаление атомарно. Для частичного удаления укажите partial_allowed.",
    path_schema=SessionIdsPath,
    query_schema=SessionBulkDeleteQuery,
    input_schema=SessionsDeleteRequestData,
    body_required=False,
    responses={
        **_ACCESS_RESPONSES,
        **_DELETE_ERRORS,
        "deleted": ResponseSpec(codes.DELETED, "Все сессии удалены", schema=SessionsDeletedData),
        "partial": ResponseSpec(codes.PARTIALLY_DELETED, "Часть сессий удалена", schema=SessionsDeletePartialData),
        "missing": ResponseSpec(codes.OBJECT_NOT_FOUND, "Пользователь или сессия не найдены", schema=SessionsDeleteErrorData),
        "invalid": ResponseSpec(codes.VALIDATION_ERROR, "Некорректный путь или тело запроса"),
        "too_many": ResponseSpec(codes.BULK_LIMIT_EXCEEDED, "Слишком много идентификаторов"),
    },
)
async def delete_sessions_bulk(
    user_id: int,
    ids: str,
    partial_allowed: bool = Depends(_partial_request),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    id_list = parse_ids(ids, MAX_BULK)
    await _check_access_and_user(user_id, payload, db)
    succeeded: list[dict] = []
    failed: list[dict] = []
    for i in id_list:
        session = await db.get(SessionModel, i)
        if session is None or session.uid != user_id:
            err = _not_found(i)
            if not partial_allowed:
                raise err
            failed.append(failed_item(i, err))
            continue
        await db.delete(session)
        succeeded.append({"id": i})
    if succeeded:
        await db.commit()
    return bulk("sessions", succeeded, failed, op="delete", partial=partial_allowed)


@router.get("/{user_id}/sessions/{id}", dependencies=[Depends(rate_limit("users_read", capacity=60, per_minute=60))])
@describe_contract(
    schema_access=_schema_access,
    summary="Получить сессию",
    path_schema=SessionIdPath,
    responses={
        **_ACCESS_RESPONSES,
        "found": ResponseSpec(codes.SUCCESS, "Сессия найдена", schema=SessionOut, response_key="session"),
        "missing": ResponseSpec(codes.OBJECT_NOT_FOUND, "Пользователь или сессия не найдены"),
        "invalid_ids": ResponseSpec(codes.VALIDATION_ERROR, "Некорректный идентификатор"),
    },
)
async def get_session(
    user_id: int,
    id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    await _check_access_and_user(user_id, payload, db)
    session = await db.get(SessionModel, id)
    if session is None or session.uid != user_id:
        raise _not_found(id)
    return single("session", _to_out(session))


@router.delete("/{user_id}/sessions/{id}", dependencies=[Depends(rate_limit("users_write", capacity=20, per_minute=20))])
@describe_contract(
    schema_access=_schema_access,
    summary="Отозвать сессию",
    path_schema=SessionIdPath,
    query_schema=SessionBulkDeleteQuery,
    input_schema=SessionsDeleteRequestData,
    body_required=False,
    responses={
        **_ACCESS_RESPONSES,
        **_DELETE_ERRORS,
        "deleted": ResponseSpec(codes.DELETED, "Сессия отозвана", schema=SessionDeleted, response_key="session"),
        "missing": ResponseSpec(codes.OBJECT_NOT_FOUND, "Пользователь или сессия не найдены"),
        "invalid": ResponseSpec(codes.VALIDATION_ERROR, "Некорректный путь или тело запроса"),
    },
)
async def delete_session(
    user_id: int,
    id: int,
    _body: None = Depends(_optional_request_body),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    await _check_access_and_user(user_id, payload, db)
    session = await db.get(SessionModel, id)
    if session is None or session.uid != user_id:
        raise _not_found(id)
    await db.delete(session)
    await db.commit()
    return deleted("session", {"id": id})
