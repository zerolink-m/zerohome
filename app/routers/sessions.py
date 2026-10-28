from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import FORBIDDEN_RESOURCE, OBJECT_NOT_FOUND
from app.core.errors import AppError
from app.core.introspection import supports
from app.core.response import bulk, bulk_listing, deleted, failed_item, listing, single
from app.core.validation import field_error
from app.core.rate_limit import rate_limit
from app.core.security import TokenPayload
from app.db.session import get_db
from app.deps.auth import require_access
from app.deps.list_query import ListQuery, list_query
from app.models.session import Session as SessionModel
from app.models.user import User
from app.deps.ids import parse_ids, partial_query
from app.routers.users import MAX_BULK
from app.schemas.session import SessionOut

router = APIRouter(prefix="/v1/users", tags=["sessions"])

SESSIONS_COLUMNS = ["id", "uid", "ua", "ip", "last_login", "added_at", "updated_at"]
sessions_query = list_query(model=SessionModel, sortable=SESSIONS_COLUMNS, searchable=SESSIONS_COLUMNS)


def _to_out(session: SessionModel) -> dict:
    return SessionOut(
        id=session.id,
        uid=session.uid,
        ua=session.ua,
        ip=session.ip,
        last_login=session.last_login,
        added_at=session.added_at,
        updated_at=session.updated_at,
    ).model_dump()


async def _check_access_and_user(
    user_id: int, payload: TokenPayload, db: AsyncSession
) -> None:
    if payload.uid != user_id and payload.role != "admin":
        raise AppError(FORBIDDEN_RESOURCE, "Доступ к чужим сессиям запрещён")
    user = await db.get(User, user_id)
    if user is None:
        raise AppError(OBJECT_NOT_FOUND, "Пользователь не найден", path={
            "user_id": field_error("object not found", type="int", value=user_id)})


def _not_found(id) -> AppError:
    return AppError(OBJECT_NOT_FOUND, "Сессия не найдена", path={
        "id": field_error("object not found", type="int", value=id)})


@router.get("/{user_id}/sessions", dependencies=[Depends(rate_limit("users_read", capacity=60, per_minute=60))])
async def list_sessions(
    user_id: int,
    q: ListQuery = Depends(sessions_query),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    await _check_access_and_user(user_id, payload, db)
    rows, total = await q.paginate(db, select(SessionModel).where(SessionModel.uid == user_id))
    return listing("sessions", [_to_out(s) for s in rows], query=q, total=total)


@router.get("/{user_id}/sessions/bulk/{ids}", dependencies=[Depends(rate_limit("users_read", capacity=60, per_minute=60))])
@supports("bulk", max_bulk=MAX_BULK)
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
async def delete_sessions_bulk(
    user_id: int,
    ids: str,
    partial_allowed: bool = Depends(partial_query),
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
async def delete_session(
    user_id: int,
    id: int,
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
