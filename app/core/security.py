from __future__ import annotations

import asyncio
import hashlib
import secrets
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

# Legacy refresh JWT signing key is retained only for one-time migrations.


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


def create_session_token(uid: int, sid: int) -> str:
    """Legacy refresh signer retained for migration regression tests only."""
    now = int(time.time())
    return jwt.encode({"uid": uid, "sid": sid, "role": "user", "iat": now,
                       "exp": now + settings.SESSION_TOKEN_TTL_SECONDS},
                      settings.JWT_SECRET_SESSION, algorithm=settings.JWT_ALGORITHM)


def new_bearer_token() -> str:
    return secrets.token_urlsafe(32)


def bearer_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_opaque_token(token: str) -> bool:
    # 32 CSPRNG bytes in canonical unpadded base64url encoding.
    return len(token) == 43 and all(c.isalnum() or c in "-_" for c in token)


def decode_legacy_refresh(token: str) -> TokenPayload:
    try:
        data = jwt.decode(token, settings.JWT_SECRET_SESSION, algorithms=[settings.JWT_ALGORITHM])
        return TokenPayload(kind="session", uid=int(data["uid"]), sid=int(data["sid"]),
                            role="user", iat=int(data["iat"]), exp=int(data["exp"]))
    except (PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise _token_error(exc)


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
