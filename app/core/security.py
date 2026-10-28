from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from jwt import PyJWTError
from jwt.exceptions import DecodeError, ExpiredSignatureError, InvalidAlgorithmError, InvalidSignatureError

from app.core.codes import (
    TOKEN_BAD_ALGORITHM,
    TOKEN_BAD_SIGNATURE,
    TOKEN_ERROR,
    TOKEN_EXPIRED,
    TOKEN_FORMAT_INVALID,
)
from app.core.config import settings
from app.core.errors import AppError

_hasher = PasswordHasher()

# Which JWT secret signs/verifies each token kind. access-admin uses its own
# secret so an access token can never be replayed as an admin one.
SECRET_BY_KIND: dict[str, str] = {
    "session": settings.JWT_SECRET_SESSION,
    "access": settings.JWT_SECRET_ACCESS,
    "access-admin": settings.JWT_SECRET_ACCESS_ADMIN,
}


@dataclass
class TokenPayload:
    kind: str
    uid: int
    sid: int
    role: str
    iat: int
    exp: int


@dataclass
class ShareTokenPayload:
    sha_id: int
    user_id: int
    type: str
    device_type: str
    device_id: int
    filename: str | None
    iat: int
    exp: int


def _verify_password_sync(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password: str, hashed: str) -> bool:
    return await asyncio.to_thread(_verify_password_sync, password, hashed)


def _encode(kind: str, uid: int, sid: int, role: str, ttl_seconds: int) -> str:
    now = int(time.time())
    payload = {
        "uid": uid,
        "sid": sid,
        "role": role,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, SECRET_BY_KIND[kind], algorithm=settings.JWT_ALGORITHM)


def create_session_token(uid: int, sid: int) -> str:
    return _encode("session", uid, sid, "user", settings.SESSION_TOKEN_TTL_SECONDS)


def create_access_token(uid: int, sid: int, role: str) -> str:
    kind = "access-admin" if role == "admin" else "access"
    return _encode(kind, uid, sid, role, settings.ACCESS_TOKEN_TTL_SECONDS)


def create_share_token(
    sha_id: int,
    user_id: int,
    type: str,
    device_type: str,
    device_id: int,
    filename: str | None,
    ttl_seconds: int,
) -> str:
    now = int(time.time())
    payload = {
        "sha_id": sha_id,
        "user_id": user_id,
        "type": type,
        "device_type": device_type,
        "device_id": device_id,
        "filename": filename,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, settings.JWT_SECRET_SHARE, algorithm=settings.JWT_ALGORITHM)


def decode_share_token(token: str) -> ShareTokenPayload:
    try:
        data = jwt.decode(
            token, settings.JWT_SECRET_SHARE, algorithms=[settings.JWT_ALGORITHM]
        )
        return ShareTokenPayload(
            sha_id=data["sha_id"],
            user_id=data["user_id"],
            type=data["type"],
            device_type=data["device_type"],
            device_id=data["device_id"],
            filename=data.get("filename"),
            iat=data["iat"],
            exp=data["exp"],
        )
    except InvalidSignatureError:
        raise AppError(TOKEN_BAD_SIGNATURE, "Токен подписан неверным ключом")
    except (ExpiredSignatureError, InvalidAlgorithmError, DecodeError, PyJWTError, KeyError) as exc:
        raise _token_error(exc)


def _token_error(exc: Exception) -> AppError:
    """Map a PyJWT failure to a cursedapi auth code (042-045, 149)."""
    if isinstance(exc, ExpiredSignatureError):
        return AppError(TOKEN_EXPIRED, "Токен истёк")
    if isinstance(exc, InvalidAlgorithmError):
        return AppError(TOKEN_BAD_ALGORITHM, "Недопустимый алгоритм токена")
    if isinstance(exc, InvalidSignatureError):
        return AppError(TOKEN_BAD_SIGNATURE, "Токен подписан неверным ключом")
    if isinstance(exc, DecodeError):
        return AppError(TOKEN_FORMAT_INVALID, "Неверный формат токена")
    return AppError(TOKEN_ERROR, "Токен невалиден")


def decode_any(token: str) -> TokenPayload:
    """Try every known secret; whichever verifies determines the token's kind."""
    bad_signature = False
    for kind, secret in SECRET_BY_KIND.items():
        try:
            data = jwt.decode(token, secret, algorithms=[settings.JWT_ALGORITHM])
            return TokenPayload(
                kind=kind,
                uid=data["uid"],
                sid=data["sid"],
                # Role comes from the token kind (i.e. which secret signed it),
                # never from a claim: only the access-admin secret makes an admin.
                role="admin" if kind == "access-admin" else "user",
                iat=data["iat"],
                exp=data["exp"],
            )
        except InvalidSignatureError:
            bad_signature = True
            continue
        except KeyError:
            raise AppError(TOKEN_ERROR, "В токене отсутствуют обязательные поля")
        except PyJWTError as exc:
            raise _token_error(exc)
    if bad_signature:
        raise AppError(TOKEN_BAD_SIGNATURE, "Токен подписан неверным ключом")
    raise AppError(TOKEN_ERROR, "Токен невалиден")
