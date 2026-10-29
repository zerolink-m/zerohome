from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import FORBIDDEN_ROLE, TOKEN_ERROR, TOKEN_MISSING
from app.core.errors import AppError
from app.core.security import (
    ShareTokenPayload,
    TokenPayload,
    decode_any,
    decode_share_token,
)
from app.db.session import get_db
from app.models.sharing import Sharing


_AUTH_HDR = {"Authorization": {"error": "token is required", "required": True, "type": "string"}}


def get_bearer_token(authorization: str | None = Header(None)) -> str:
    if not authorization:
        raise AppError(TOKEN_MISSING, "Токен отсутствует", error_headers=_AUTH_HDR)
    if not authorization.startswith("Bearer "):
        raise AppError(TOKEN_ERROR, "Неподдерживаемая схема авторизации (ожидается Bearer)")
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise AppError(TOKEN_MISSING, "Токен отсутствует", error_headers=_AUTH_HDR)
    return token


def get_bearer_token_or_query(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
) -> str:
    """Same as get_bearer_token, but also accepts ?token= — for endpoints embedded
directly into <video>/<img> src, where an Authorization header can't be set."""
    return extract_token_flexible(authorization, token)


def _extract_token(authorization: str | None, token: str | None) -> str:
    if authorization and authorization.startswith("Bearer "):
        candidate = authorization[len("Bearer "):].strip()
        if candidate:
            return candidate
    if token:
        return token
    raise AppError(TOKEN_MISSING, "Токен отсутствует", error_headers=_AUTH_HDR)


def require_session(
    token: str = Depends(get_bearer_token),
) -> TokenPayload:
    """+auth-session: only POST /v1/auth/access accepts this dependency."""
    payload = decode_any(token)
    if payload.kind != "session":
        raise AppError(TOKEN_ERROR, "Требуется session-токен")
    return payload


def require_access(
    token: str = Depends(get_bearer_token),
) -> TokenPayload:
    """+auth-access: any valid access token, admin or user role."""
    payload = decode_any(token)
    if payload.kind not in ("access", "access-admin"):
        raise AppError(TOKEN_ERROR, "Требуется access-токен")
    return payload


def require_access_qs(
    token: str = Depends(get_bearer_token_or_query),
) -> TokenPayload:
    """+auth-access: same as require_access, but token may come from ?token= too."""
    payload = decode_any(token)
    if payload.kind not in ("access", "access-admin"):
        raise AppError(TOKEN_ERROR, "Требуется access-токен")
    return payload


def require_access_admin(
    token: str = Depends(get_bearer_token),
) -> TokenPayload:
    """+auth-access-admin: valid access token with role=admin."""
    payload = decode_any(token)
    if payload.kind == "access-admin":
        return payload
    if payload.kind == "access":
        raise AppError(FORBIDDEN_ROLE, "Недостаточно роли для эндпоинта")
    raise AppError(TOKEN_ERROR, "Требуется access-токен")


@dataclass
class AccessContext:
    """Either a normal logged-in user (payload) or a validated share token (share) — never both."""
    payload: TokenPayload | None
    share: ShareTokenPayload | None


def _try_decode_any(token: str) -> TokenPayload | None:
    try:
        return decode_any(token)
    except AppError:
        return None


async def authorize_device_token(
    raw: str, device_type: str, share_type: str, id: int, db: AsyncSession
) -> AccessContext:
    """Access token, OR a share-token of the given (device_type, share_type) scoped to
device `id` (checked against the sharing row, so revoked links are rejected).
Shared by the HTTP resolvers and the WebRTC websocket."""
    payload = _try_decode_any(raw)
    if payload is not None and payload.kind in ("access", "access-admin"):
        return AccessContext(payload=payload, share=None)

    share = decode_share_token(raw)
    if (
        share.type != share_type
        or share.device_type != device_type
        or share.device_id != id
    ):
        raise AppError(TOKEN_ERROR, "Токен доступа не подходит для этого ресурса")

    row = await db.get(Sharing, share.sha_id)
    if (
        row is None
        or row.device_type != share.device_type
        or row.device_id != share.device_id
        or row.type != share.type
    ):
        raise AppError(TOKEN_ERROR, "Ссылка доступа отозвана или невалидна")

    return AccessContext(payload=None, share=share)


async def resolve_webrtc_ice_access(
    token: str = Depends(get_bearer_token),
    db: AsyncSession = Depends(get_db),
) -> AccessContext:
    """Endpoints without a camera id that serve WebRTC clients (ICE servers): any access
token, OR a live 'stream' share-token of any camera (the only share that opens WebRTC)."""
    payload = _try_decode_any(token)
    if payload is not None and payload.kind in ("access", "access-admin"):
        return AccessContext(payload=payload, share=None)

    share = decode_share_token(token)
    if share.type != "stream" or share.device_type != "cams":
        raise AppError(TOKEN_ERROR, "Токен доступа не подходит для этого ресурса")
    row = await db.get(Sharing, share.sha_id)
    if (
        row is None
        or row.device_type != share.device_type
        or row.device_id != share.device_id
        or row.type != share.type
    ):
        raise AppError(TOKEN_ERROR, "Ссылка доступа отозвана или невалидна")
    return AccessContext(payload=None, share=share)


def _make_share_gated_resolver(device_type: str, share_type: str):
    """Builds a dependency that lets an access token OR a share-token of the given
(device_type, share_type) pair in. Used to gate the "device-use"-style endpoints
where the share meaningfully grants active control of one device:

- cams stream + PTZ  → device_type='cams',     share_type='stream'
- switches state r/w → device_type='switches', share_type='device_use'"""

    async def _resolve(
        id: int,
        authorization: str | None = Header(None),
        token: str | None = Query(None),
        db: AsyncSession = Depends(get_db),
    ) -> AccessContext:
        raw = extract_token_flexible(authorization, token)
        return await authorize_device_token(raw, device_type, share_type, id, db)

    return _resolve


resolve_stream_access = _make_share_gated_resolver("cams", "stream")
resolve_device_use_access = _make_share_gated_resolver("switches", "device_use")


def _decode_dashed(token: str, chunk: int = 10, sep: str = "-") -> str:
    """Removes the '-' the client inserts after every `chunk` characters of real token
data (positions chunk, 2*chunk+1, ... — 1-indexed within the data stream). This is
purely positional: a '-' is only ever removed once exactly `chunk` data characters
have been collected since the last removal, so a literal '-' occurring earlier is
left in place as data, never stripped.

This exists because the CDN/WAF in front of this API blocks query strings containing
a contiguous alphanumeric run of 16+ characters, which a raw JWT easily exceeds;
inserting a separator every 10 characters keeps every run under that limit."""
    out: list[str] = []
    count = 0
    i = 0
    n = len(token)
    while i < n:
        out.append(token[i])
        count += 1
        i += 1
        if count == chunk:
            count = 0
            if i < n and token[i] == sep:
                i += 1
    return "".join(out)


def _query_token_candidates(token: str) -> list[str]:
    """Variants of a ?token= value, in the order they are tried: as-is first (a plain
token; base64url JWTs may contain a literal '-', so it must not be stripped blindly),
then the de-dashed form. Deduplicated; the last one is the 'dashed' reading."""
    decoded = _decode_dashed(token)
    return [token, decoded] if decoded != token else [token]


def _is_verifiable_token(candidate: str) -> bool:
    if _try_decode_any(candidate) is not None:
        return True
    try:
        decode_share_token(candidate)
        return True
    except AppError:
        return False


def extract_token_flexible(authorization: str | None, token: str | None) -> str:
    """Like _extract_token, but a ?token= value may be plain OR dash-encoded (see
_decode_dashed). A valid Authorization header always wins and is never altered. For
the query value, each variant is checked against the real signature/expiry
verification, so nothing is weakened; if none verifies, the de-dashed reading is
returned, so downstream produces the same error as before."""
    if authorization and authorization.startswith("Bearer "):
        candidate = authorization[len("Bearer "):].strip()
        if candidate:
            return candidate
    if not token:
        raise AppError(TOKEN_MISSING, "Токен отсутствует", error_headers=_AUTH_HDR)
    candidates = _query_token_candidates(token)
    for c in candidates:
        if len(candidates) == 1 or _is_verifiable_token(c):
            return c
    return candidates[-1]


async def resolve_record_access(
    id: int,
    recording_id: str,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> AccessContext:
    """Accepts a normal access token, OR a type=record share token scoped to this exact
cam+filename (grants that one recording's file/snapshot/meta only). The token travels
as ?token= (query, not a path segment) with a '-' inserted every 10 characters by the
client to dodge the CDN/WAF's long-alphanumeric-run filter — see _decode_dashed.
Authorization header still works too, is not dash-encoded, and takes priority."""
    raw = extract_token_flexible(authorization, token)

    payload = _try_decode_any(raw)
    if payload is not None and payload.kind in ("access", "access-admin"):
        return AccessContext(payload=payload, share=None)

    share = decode_share_token(raw)
    if (
        share.type != "record"
        or share.device_type != "cams"
        or share.device_id != id
        or share.filename != recording_id
    ):
        raise AppError(TOKEN_ERROR, "Токен доступа не подходит для этого ресурса")

    row = await db.get(Sharing, share.sha_id)
    if (
        row is None
        or row.device_type != share.device_type
        or row.device_id != share.device_id
        or row.type != share.type
        or row.filename != share.filename
    ):
        raise AppError(TOKEN_ERROR, "Ссылка доступа отозвана или невалидна")

    return AccessContext(payload=None, share=share)
