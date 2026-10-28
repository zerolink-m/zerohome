import re
import time

from fastapi import APIRouter, Depends, Request, BackgroundTasks
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import AUTHORIZED, BAD_CREDENTIALS, EMAIL_TAKEN, LOGGED_OUT, TOKEN_REFRESHED, TOKEN_ERROR
from app.core.errors import AppError
from app.core.response import created, success
from app.core.ip import resolve_ip
from app.core.rate_limit import rate_limit
from app.core.security import create_access_token, create_session_token, hash_password, verify_password
from app.db.session import get_db, AsyncSessionLocal
from app.deps.auth import require_access, require_session
from app.models.session import Session as SessionModel
from app.models.user import User
from app.deps.request_body import SingleBody, single_body
from app.schemas.auth import LoginRequest, RegisterRequest, TokenOut

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post(
    "/register",
    dependencies=[Depends(rate_limit("auth_register", capacity=3, per_minute=0.3))],
)
async def register(
    req: SingleBody[RegisterRequest] = Depends(single_body("user", RegisterRequest)),
    db: AsyncSession = Depends(get_db),
):
    body = req.obj
    existing = await db.scalar(select(User).where(User.email == body.email))
    if existing is not None:
        raise AppError(EMAIL_TAKEN, "Email уже занят")

    now = int(time.time())
    user = User(
        name=body.name,
        surname=body.surname,
        email=body.email,
        password=await hash_password(body.password),
        role="user",
        added_at=now,
        updated_at=now,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AppError(EMAIL_TAKEN, "Email уже занят")

    return created("user", {"id": user.id})


@router.post("/login", dependencies=[Depends(rate_limit("auth_login", capacity=5, per_minute=1))])
async def login(
    request: Request,
    req: SingleBody[LoginRequest] = Depends(single_body("credentials", LoginRequest)),
    db: AsyncSession = Depends(get_db),
):
    body = req.obj
    user = await db.scalar(select(User).where(User.email == body.email))
    if user is None or not await verify_password(body.password, user.password):
        raise AppError(BAD_CREDENTIALS, "Неверный email или пароль")

    now = int(time.time())
    session = SessionModel(
        uid=user.id,
        ua=request.headers.get("user-agent", ""),
        ip=resolve_ip(request),
        last_login=now,
        added_at=now,
        updated_at=now,
    )
    db.add(session)
    await db.commit()

    token = create_session_token(uid=user.id, sid=session.id)
    return success(TokenOut(token=token).model_dump(), code=AUTHORIZED)


def _strip_digits(s: str) -> str:
    return re.sub(r'\d+', '', s)


async def _update_session_ua(sid: int, new_ua: str):
    async with AsyncSessionLocal() as db:
        session = await db.get(SessionModel, sid)
        if session is not None:
            session.ua = new_ua
            session.updated_at = int(time.time())
            await db.commit()


@router.post("/access", dependencies=[Depends(rate_limit("auth_access", capacity=20, per_minute=20))])
async def exchange_access(
    request: Request,
    background_tasks: BackgroundTasks,
    payload=Depends(require_session),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(SessionModel, payload.sid)
    if session is None:
        raise AppError(TOKEN_ERROR, "Сессия не найдена")

    ua = request.headers.get("user-agent", "")

    if session.ua != ua:
        if _strip_digits(session.ua) == _strip_digits(ua):
            background_tasks.add_task(_update_session_ua, session.id, ua)
        else:
            await db.delete(session)
            await db.commit()
            raise AppError(TOKEN_ERROR, "User-Agent не совпадает с сессией — сессия удалена")

    session.ip = resolve_ip(request)
    session.last_login = int(time.time())
    session.updated_at = int(time.time())
    await db.commit()

    user = await db.get(User, session.uid)
    if user is None:
        raise AppError(TOKEN_ERROR, "Пользователь не найден")

    token = create_access_token(uid=user.id, sid=session.id, role=user.role)
    return success(TokenOut(token=token).model_dump(), code=TOKEN_REFRESHED)


@router.post("/logout", dependencies=[Depends(rate_limit("auth_logout", capacity=30, per_minute=30))])
async def logout(payload=Depends(require_access), db: AsyncSession = Depends(get_db)):
    session = await db.get(SessionModel, payload.sid)
    if session is not None:
        await db.delete(session)
        await db.commit()
    return success({"session": {"id": payload.sid}}, code=LOGGED_OUT)
