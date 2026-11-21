import hashlib
import time

import jwt
import pytest

from app.core import security as sec
from app.core.config import settings
from app.core.errors import AppError


async def test_password_hash_and_verify():
    hashed = await sec.hash_password("secret")
    assert hashed != "secret" and hashed.startswith("$argon2")
    assert await sec.verify_password("secret", hashed)
    assert not await sec.verify_password("wrong", hashed)
    assert not await sec.verify_password("x", "invalid")
    assert await sec.hash_password("same") != await sec.hash_password("same")


def test_opaque_tokens_are_random_and_hash_only():
    a, b = sec.new_bearer_token(), sec.new_bearer_token()
    assert a != b and sec.is_opaque_token(a) and sec.is_opaque_token(b)
    assert sec.bearer_hash(a) == hashlib.sha256(a.encode()).hexdigest()
    assert not sec.is_opaque_token(sec.create_session_token(1, 1))


def test_legacy_refresh_validation():
    token = sec.create_session_token(5, 7)
    payload = sec.decode_legacy_refresh(token)
    assert (payload.uid, payload.sid, payload.kind) == (5, 7, "session")
    assert payload.exp - payload.iat == settings.SESSION_TOKEN_TTL_SECONDS
    now = int(time.time())
    expired = jwt.encode({"uid": 1, "sid": 1, "iat": now - 100, "exp": now - 1},
                         settings.JWT_SECRET_SESSION, algorithm="HS256")
    with pytest.raises(AppError) as exc:
        sec.decode_legacy_refresh(expired)
    assert exc.value.code == "043"
    with pytest.raises(AppError):
        sec.decode_legacy_refresh(jwt.encode({"uid": 1, "sid": 1, "exp": now + 100},
                                              "test-legacy-access-secret-for-rejection-only", algorithm="HS256"))


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c"])
def test_garbage_refresh_rejected(token):
    with pytest.raises(AppError):
        sec.decode_legacy_refresh(token)


def test_share_token_roundtrip_and_expiry():
    token = sec.create_share_token(3, 4, "record", "cams", 9, "f.mp4", 120)
    payload = sec.decode_share_token(token)
    assert (payload.sha_id, payload.user_id, payload.filename) == (3, 4, "f.mp4")
    assert payload.exp - payload.iat == 120
    with pytest.raises(AppError) as exc:
        sec.decode_share_token(sec.create_share_token(1, 1, "stream", "cams", 1, None, -5))
    assert exc.value.code == "043"
    with pytest.raises(AppError):
        sec.decode_share_token(sec.create_session_token(1, 1))
