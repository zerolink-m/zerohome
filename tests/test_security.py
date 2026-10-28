import time

import jwt
import pytest

from app.core import security as sec

from app.core.config import settings
from app.core.errors import AppError


async def test_password_hash_and_verify():
    h = await sec.hash_password("secret")
    assert h != "secret" and h.startswith("$argon2")
    assert await sec.verify_password("secret", h) is True
    assert await sec.verify_password("wrong", h) is False


async def test_verify_password_invalid_hash_is_false():
    assert await sec.verify_password("x", "not-a-hash") is False


async def test_hash_is_salted():
    assert await sec.hash_password("a") != await sec.hash_password("a")


def test_session_token_roundtrip():
    p = sec.decode_any(sec.create_session_token(5, 7))
    assert (p.kind, p.uid, p.sid, p.role) == ("session", 5, 7, "user")
    assert p.exp - p.iat == settings.SESSION_TOKEN_TTL_SECONDS


def test_access_token_user_and_admin_kinds():
    u = sec.decode_any(sec.create_access_token(1, 2, "user"))
    a = sec.decode_any(sec.create_access_token(1, 2, "admin"))
    assert (u.kind, u.role) == ("access", "user")
    assert (a.kind, a.role) == ("access-admin", "admin")
    assert u.exp - u.iat == settings.ACCESS_TOKEN_TTL_SECONDS


def test_admin_token_signed_with_distinct_secret():
    tok = sec.create_access_token(1, 2, "admin")
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(tok, settings.JWT_SECRET_ACCESS, algorithms=["HS256"])
    jwt.decode(tok, settings.JWT_SECRET_ACCESS_ADMIN, algorithms=["HS256"])


def test_secrets_are_all_different():
    s = {settings.JWT_SECRET_SESSION, settings.JWT_SECRET_ACCESS,
         settings.JWT_SECRET_ACCESS_ADMIN, settings.JWT_SECRET_SHARE}
    assert len(s) == 4


def test_forged_admin_role_in_access_token_does_not_make_admin_kind():
    # attacker signs role=admin with the ordinary access secret
    now = int(time.time())
    tok = jwt.encode({"uid": 1, "sid": 1, "role": "admin", "iat": now, "exp": now + 100},
                     settings.JWT_SECRET_ACCESS, algorithm="HS256")
    assert sec.decode_any(tok).kind == "access"


def test_expired_token_rejected():
    now = int(time.time())
    tok = jwt.encode({"uid": 1, "sid": 1, "role": "user", "iat": now - 100, "exp": now - 10},
                     settings.JWT_SECRET_ACCESS, algorithm="HS256")
    with pytest.raises(AppError) as e:
        sec.decode_any(tok)
    assert e.value.code == "043"


@pytest.mark.parametrize("tok", ["", "garbage", "a.b.c"])
def test_garbage_token_rejected(tok):
    with pytest.raises(AppError) as e:
        sec.decode_any(tok)
    assert e.value.code == "149"


def test_wrong_secret_rejected():
    now = int(time.time())
    tok = jwt.encode({"uid": 1, "sid": 1, "role": "user", "iat": now, "exp": now + 100},
                     "other-secret-other-secret-other-secret", algorithm="HS256")
    with pytest.raises(AppError):
        sec.decode_any(tok)


def test_alg_none_rejected():
    now = int(time.time())
    tok = jwt.encode({"uid": 1, "sid": 1, "role": "admin", "iat": now, "exp": now + 100},
                     None, algorithm="none")
    with pytest.raises(AppError):
        sec.decode_any(tok)


def test_missing_claims_rejected():
    now = int(time.time())
    tok = jwt.encode({"uid": 1, "exp": now + 100}, settings.JWT_SECRET_ACCESS, algorithm="HS256")
    with pytest.raises(AppError):
        sec.decode_any(tok)


def test_share_token_roundtrip():
    t = sec.create_share_token(3, 4, "record", "cams", 9, "f.mp4", 120)
    p = sec.decode_share_token(t)
    assert (p.sha_id, p.user_id, p.type, p.device_type, p.device_id, p.filename) == \
        (3, 4, "record", "cams", 9, "f.mp4")
    assert p.exp - p.iat == 120


def test_share_token_without_filename():
    p = sec.decode_share_token(sec.create_share_token(1, 1, "stream", "cams", 1, None, 60))
    assert p.filename is None


def test_share_token_expired():
    t = sec.create_share_token(1, 1, "stream", "cams", 1, None, -5)
    with pytest.raises(AppError) as e:
        sec.decode_share_token(t)
    assert e.value.code == "043"


def test_share_and_access_tokens_not_interchangeable():
    with pytest.raises(AppError):
        sec.decode_share_token(sec.create_access_token(1, 1, "user"))
    with pytest.raises(AppError):
        sec.decode_any(sec.create_share_token(1, 1, "stream", "cams", 1, None, 60))
