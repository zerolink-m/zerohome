"""Session creation, one-time legacy refresh conversion and revocation."""
import asyncio
import logging
import re
import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import (
    AUTHORIZED, BAD_CREDENTIALS, CREATED, EMAIL_TAKEN, LOGGED_OUT,
    TOKEN_ERROR, TOKEN_EXPIRED, TOKEN_REFRESHED,
)
from app.core.errors import AppError
from app.core.response import success
from app.core.ip import resolve_ip
from app.core.rate_limit import rate_limit
from app.core.security import bearer_hash, decode_legacy_refresh, hash_password, new_bearer_token, verify_password
from app.db.session import AsyncSessionLocal, get_db
from app.deps.auth import IDLE_SECONDS, get_bearer_token, require_access
from app.models.session import Session as SessionModel
from app.models.user import User
from app.deps.request_body import SingleBody, single_body
from app.schemas.auth import LoginRequest, RegisterRequest, TokenOut

router = APIRouter(prefix="/v1/auth", tags=["auth"])
log = logging.getLogger(__name__)
_cleanup_task: asyncio.Task | None = None


def _request_body(key, schema):
    """Auth is a virtual/custom API: its body is an envelope, not CRUD partial."""
    parse = single_body(key, schema)

    async def dependency(request: Request):
        return await parse(request)

    return dependency


async def _cleanup_sessions() -> None:
    while True:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(delete(SessionModel).where(
                    SessionModel.last_seen <= int(time.time()) - IDLE_SECONDS))
                if result.rowcount:
                    await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Session expiration cleanup failed")
        await asyncio.sleep(3600)


def start_session_cleanup() -> None:
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_sessions())


async def stop_session_cleanup() -> None:
    global _cleanup_task
    if _cleanup_task is not None:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None


def _new_session(user: User, request: Request, now: int, token: str) -> SessionModel:
    return SessionModel(uid=user.id, ua=request.headers.get("user-agent", ""),
                        ip=resolve_ip(request), last_login=now, last_seen=now,
                        added_at=now, updated_at=now, token_hash=bearer_hash(token))


@router.post("/register", dependencies=[Depends(rate_limit("auth_register", capacity=3, per_minute=0.3))])
async def register(request: Request,
                   req: SingleBody[RegisterRequest] = Depends(_request_body("user", RegisterRequest)),
                   db: AsyncSession = Depends(get_db)):
    body = req.obj
    if await db.scalar(select(User.id).where(User.email == body.email)) is not None:
        raise AppError(EMAIL_TAKEN, "Email уже занят")
    now = int(time.time())
    user = User(name=body.name, surname=body.surname, email=body.email,
                password=await hash_password(body.password), role="user", added_at=now, updated_at=now)
    db.add(user)
    token = new_bearer_token()
    try:
        await db.flush()
        db.add(_new_session(user, request, now, token))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AppError(EMAIL_TAKEN, "Email уже занят")
    return success({"user": {"id": user.id}, "token": token}, code=CREATED)


@router.post("/login", dependencies=[Depends(rate_limit("auth_login", capacity=5, per_minute=1))])
async def login(request: Request,
                req: SingleBody[LoginRequest] = Depends(_request_body("credentials", LoginRequest)),
                db: AsyncSession = Depends(get_db)):
    body = req.obj
    user = await db.scalar(select(User).where(User.email == body.email))
    if user is None or not await verify_password(body.password, user.password):
        raise AppError(BAD_CREDENTIALS, "Неверный email или пароль")
    token = new_bearer_token()
    db.add(_new_session(user, request, int(time.time()), token))
    await db.commit()
    return success(TokenOut(token=token).model_dump(), code=AUTHORIZED)


@router.post("/new-refresh", dependencies=[Depends(rate_limit("auth_new_refresh", capacity=20, per_minute=20))])
async def new_refresh(request: Request, token: str = Depends(get_bearer_token),
                      db: AsyncSession = Depends(get_db)):
    """One-time exchange of a still-valid legacy session JWT; no access JWT is accepted."""
    payload = decode_legacy_refresh(token)
    now = int(time.time())
    row = await db.scalar(select(SessionModel).where(SessionModel.id == payload.sid,
                                                    SessionModel.uid == payload.uid))
    if row is None or row.token_hash is not None:
        raise AppError(TOKEN_ERROR, "Сессия не найдена или уже обновлена")
    if row.last_seen <= now - IDLE_SECONDS:
        await db.execute(delete(SessionModel).where(SessionModel.id == row.id,
                                                     SessionModel.last_seen <= now - IDLE_SECONDS))
        await db.commit()
        raise AppError(TOKEN_EXPIRED, "Сессия истекла")
    ua = request.headers.get("user-agent", "")
    if re.sub(r"\d+", "", row.ua) != re.sub(r"\d+", "", ua):
        await db.execute(delete(SessionModel).where(SessionModel.id == row.id, SessionModel.token_hash.is_(None)))
        await db.commit()
        raise AppError(TOKEN_ERROR, "User-Agent не совпадает с сессией — сессия удалена")
    new_token = new_bearer_token()
    result = await db.execute(update(SessionModel).where(
        SessionModel.id == row.id, SessionModel.uid == payload.uid,
        SessionModel.token_hash.is_(None), SessionModel.last_seen > now - IDLE_SECONDS
    ).values(token_hash=bearer_hash(new_token), ua=ua, ip=resolve_ip(request),
             last_login=now, last_seen=now, updated_at=now))
    if result.rowcount != 1:
        await db.rollback()
        raise AppError(TOKEN_ERROR, "Сессия уже обновлена или отозвана")
    await db.commit()
    return success(TokenOut(token=new_token).model_dump(), code=TOKEN_REFRESHED)


@router.post("/logout", dependencies=[Depends(rate_limit("auth_logout", capacity=30, per_minute=30))])
async def logout(payload=Depends(require_access), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(SessionModel).where(SessionModel.id == payload.sid))
    await db.commit()
    return success({"session": {"id": payload.sid}}, code=LOGGED_OUT)
