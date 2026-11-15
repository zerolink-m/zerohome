"""Database-backed opaque bearer authentication and scoped share-token resolution."""
from __future__ import annotations

import hmac
import re
import time
from dataclasses import dataclass

from fastapi import Depends, Header, Query, Request
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import FORBIDDEN_ROLE, TOKEN_ERROR, TOKEN_EXPIRED, TOKEN_MISSING
from app.core.errors import AppError
from app.core.security import ShareTokenPayload, TokenPayload, bearer_hash, decode_share_token, is_opaque_token
from app.db.session import get_db
from app.models.session import Session
from app.models.sharing import Sharing
from app.models.user import User

_AUTH_HDR = {"Authorization": {"error": "token is required", "required": True, "type": "string"}}
IDLE_SECONDS = 30 * 24 * 3600
ACTIVITY_INTERVAL = 60


def get_bearer_token(authorization: str | None = Header(None)) -> str:
    if not authorization:
        raise AppError(TOKEN_MISSING, "Токен отсутствует", error_headers=_AUTH_HDR)
    if not authorization.startswith("Bearer "):
        raise AppError(TOKEN_ERROR, "Неподдерживаемая схема авторизации (ожидается Bearer)")
    token = authorization[7:].strip()
    if not token:
        raise AppError(TOKEN_MISSING, "Токен отсутствует", error_headers=_AUTH_HDR)
    return token


def _decode_dashed(token: str, chunk: int = 10, sep: str = "-") -> str:
    out = []
    count = 0
    i = 0
    while i < len(token):
        out.append(token[i])
        count += 1
        i += 1
        if count == chunk:
            count = 0
            if i < len(token) and token[i] == sep:
                i += 1
    return "".join(out)


def _query_token_candidates(token: str) -> list[str]:
    decoded = _decode_dashed(token)
    return [token, decoded] if decoded != token else [token]


def extract_token_flexible(authorization: str | None, token: str | None) -> str:
    if authorization and authorization.startswith("Bearer ") and authorization[7:].strip():
        return authorization[7:].strip()
    if not token:
        raise AppError(TOKEN_MISSING, "Токен отсутствует", error_headers=_AUTH_HDR)
    return token


def get_bearer_token_or_query(authorization: str | None = Header(None), token: str | None = Query(None)) -> str:
    return extract_token_flexible(authorization, token)


async def _authenticate(raw: str, db: AsyncSession, ua: str, query: bool = False,
                        retry: bool = True) -> TokenPayload:
    now = int(time.time())
    candidates = _query_token_candidates(raw) if query else [raw]
    for candidate in candidates:
        if not is_opaque_token(candidate):
            continue
        digest = bearer_hash(candidate)
        row = await db.scalar(select(Session).where(Session.token_hash == digest))
        if row is None or not hmac.compare_digest(row.token_hash, digest):
            continue
        cutoff = now - IDLE_SECONDS
        if row.last_seen <= cutoff:
            result = await db.execute(delete(Session).where(Session.id == row.id, Session.last_seen <= cutoff))
            await db.commit()
            if not result.rowcount and retry:
                # A concurrent request extended activity after our initial read.
                return await _authenticate(raw, db, ua, query=query, retry=False)
            raise AppError(TOKEN_EXPIRED if result.rowcount else TOKEN_ERROR,
                           "Сессия истекла" if result.rowcount else "Сессия отозвана")
        if row.ua != ua:
            if re.sub(r"\d+", "", row.ua) != re.sub(r"\d+", "", ua):
                await db.execute(delete(Session).where(Session.id == row.id, Session.token_hash == digest))
                await db.commit()
                raise AppError(TOKEN_ERROR, "User-Agent не совпадает с сессией — сессия удалена")
            # Compatible browser version bump; update only if row is still live.
            result = await db.execute(update(Session).where(
                Session.id == row.id, Session.token_hash == digest, Session.last_seen > cutoff
            ).values(ua=ua, last_seen=now, updated_at=now))
            await db.commit()
            if not result.rowcount:
                raise AppError(TOKEN_ERROR, "Сессия отозвана")
        elif row.last_seen <= now - ACTIVITY_INTERVAL:
            result = await db.execute(update(Session).where(
                Session.id == row.id, Session.token_hash == digest, Session.last_seen > cutoff
            ).values(last_seen=now))
            await db.commit()
            if not result.rowcount:
                raise AppError(TOKEN_ERROR, "Сессия отозвана")
        user = await db.get(User, row.uid)
        if user is None:
            raise AppError(TOKEN_ERROR, "Пользователь не найден")
        return TokenPayload(kind="bearer", uid=user.id, sid=row.id, role=user.role, iat=row.added_at,
                            exp=row.last_seen + IDLE_SECONDS)
    raise AppError(TOKEN_ERROR, "Токен невалиден или отозван")


async def require_access(request: Request, token: str = Depends(get_bearer_token),
                         db: AsyncSession = Depends(get_db)) -> TokenPayload:
    return await _authenticate(token, db, request.headers.get("user-agent", ""))


async def require_access_qs(request: Request, token: str = Depends(get_bearer_token_or_query),
                            db: AsyncSession = Depends(get_db)) -> TokenPayload:
    return await _authenticate(token, db, request.headers.get("user-agent", ""), query=True)


async def require_access_admin(payload: TokenPayload = Depends(require_access)) -> TokenPayload:
    if payload.role != "admin":
        raise AppError(FORBIDDEN_ROLE, "Недостаточно роли для эндпоинта")
    return payload


@dataclass
class AccessContext:
    payload: TokenPayload | None
    share: ShareTokenPayload | None


async def _access_or_share(raw: str, db: AsyncSession, ua: str, *, query: bool = False) -> AccessContext:
    candidates = _query_token_candidates(raw) if query else [raw]
    for candidate in candidates:
        if is_opaque_token(candidate):
            try:
                return AccessContext(payload=await _authenticate(candidate, db, ua), share=None)
            except AppError as exc:
                if exc.code != TOKEN_ERROR or "невалиден или отозван" not in exc.message:
                    raise
    expired = None
    for candidate in candidates:
        try:
            share = decode_share_token(candidate)
            return AccessContext(payload=None, share=share)
        except AppError as exc:
            if exc.code == TOKEN_EXPIRED:
                expired = exc
    if expired is not None:
        raise expired
    raise AppError(TOKEN_ERROR, "Токен невалиден или отозван")


async def authorize_device_token(raw: str, device_type: str, share_type: str, id: int,
                                 db: AsyncSession, ua: str = "", query: bool = True) -> AccessContext:
    ctx = await _access_or_share(raw, db, ua, query=query)
    if ctx.payload is not None:
        return ctx
    share = ctx.share
    if share.type != share_type or share.device_type != device_type or share.device_id != id:
        raise AppError(TOKEN_ERROR, "Токен доступа не подходит для этого ресурса")
    row = await db.get(Sharing, share.sha_id)
    if row is None or row.device_type != share.device_type or row.device_id != share.device_id or row.type != share.type:
        raise AppError(TOKEN_ERROR, "Ссылка доступа отозвана или невалидна")
    return ctx


async def resolve_webrtc_ice_access(request: Request, token: str = Depends(get_bearer_token),
                                    db: AsyncSession = Depends(get_db)) -> AccessContext:
    ctx = await _access_or_share(token, db, request.headers.get("user-agent", ""))
    if ctx.payload:
        return ctx
    share = ctx.share
    if share.type != "stream" or share.device_type != "cams":
        raise AppError(TOKEN_ERROR, "Токен доступа не подходит для этого ресурса")
    row = await db.get(Sharing, share.sha_id)
    if row is None or row.type != share.type or row.device_type != share.device_type or row.device_id != share.device_id:
        raise AppError(TOKEN_ERROR, "Ссылка доступа отозвана или невалидна")
    return ctx


def _make_share_gated_resolver(device_type: str, share_type: str):
    async def _resolve(id: int, request: Request, authorization: str | None = Header(None),
                       token: str | None = Query(None), db: AsyncSession = Depends(get_db)) -> AccessContext:
        raw = extract_token_flexible(authorization, token)
        return await authorize_device_token(raw, device_type, share_type, id, db,
                                            request.headers.get("user-agent", ""), query=not bool(authorization and authorization.startswith("Bearer ") and authorization[7:].strip()))
    return _resolve


resolve_stream_access = _make_share_gated_resolver("cams", "stream")
resolve_device_use_access = _make_share_gated_resolver("switches", "device_use")


async def resolve_record_access(id: int, recording_id: str, request: Request,
                                authorization: str | None = Header(None), token: str | None = Query(None),
                                db: AsyncSession = Depends(get_db)) -> AccessContext:
    raw = extract_token_flexible(authorization, token)
    ctx = await _access_or_share(raw, db, request.headers.get("user-agent", ""),
                                 query=not bool(authorization and authorization.startswith("Bearer ") and authorization[7:].strip()))
    if ctx.payload:
        return ctx
    share = ctx.share
    if share.type != "record" or share.device_type != "cams" or share.device_id != id or share.filename != recording_id:
        raise AppError(TOKEN_ERROR, "Токен доступа не подходит для этого ресурса")
    row = await db.get(Sharing, share.sha_id)
    if row is None or row.device_type != share.device_type or row.device_id != share.device_id or row.type != share.type or row.filename != share.filename:
        raise AppError(TOKEN_ERROR, "Ссылка доступа отозвана или невалидна")
    return ctx
