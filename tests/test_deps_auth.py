import time

import jwt
import pytest

from app.core.codes import FORBIDDEN_ROLE, TOKEN_ERROR
from app.core.config import settings
from app.core.errors import AppError
from app.core.security import create_access_token, create_session_token, create_share_token
from app.deps import auth as deps


def test_bearer_parsing():
    assert deps.get_bearer_token("Bearer abc") == "abc"
    assert deps.get_bearer_token("Bearer   abc  ") == "abc"
    for bad in (None, "", "Bearer "):
        with pytest.raises(AppError) as e:
            deps.get_bearer_token(bad)
        assert e.value.code == "046", bad
    for bad in ("Basic abc", "bearer abc", "Bearer"):
        with pytest.raises(AppError) as e:
            deps.get_bearer_token(bad)
        assert e.value.code == "042", bad


def test_extract_token_header_priority_over_query():
    assert deps._extract_token("Bearer h", "q") == "h"
    assert deps._extract_token(None, "q") == "q"
    assert deps._extract_token("Bearer ", "q") == "q"
    with pytest.raises(AppError):
        deps._extract_token(None, None)


def test_require_session():
    assert deps.require_session(create_session_token(1, 1)).kind == "session"
    with pytest.raises(AppError) as e:
        deps.require_session(create_access_token(1, 1, "user"))
    assert e.value.code == TOKEN_ERROR
    with pytest.raises(AppError):
        deps.require_session(create_access_token(1, 1, "admin"))


@pytest.mark.parametrize("fn", [deps.require_access, deps.require_access_qs])
def test_require_access(fn):
    assert fn(create_access_token(1, 1, "user")).kind == "access"
    assert fn(create_access_token(1, 1, "admin")).kind == "access-admin"
    with pytest.raises(AppError) as e:
        fn(create_session_token(1, 1))
    assert e.value.code == TOKEN_ERROR


def test_require_access_admin():
    assert deps.require_access_admin(create_access_token(1, 1, "admin")).role == "admin"
    with pytest.raises(AppError) as e:
        deps.require_access_admin(create_access_token(1, 1, "user"))
    assert e.value.code == FORBIDDEN_ROLE
    with pytest.raises(AppError) as e:
        deps.require_access_admin(create_session_token(1, 1))
    assert e.value.code == TOKEN_ERROR


def test_try_decode_any_returns_none_on_garbage():
    assert deps._try_decode_any("zzz") is None
    assert deps._try_decode_any(create_share_token(1, 1, "stream", "cams", 1, None, 60)) is None


def test_decode_dashed():
    raw = "abcdefghijKLMNOPQRSTuvwxyz"
    dashed = "abcdefghij-KLMNOPQRST-uvwxyz"
    assert deps._decode_dashed(dashed) == raw
    assert deps._decode_dashed(raw) == raw  # no separators -> unchanged
    # literal '-' inside the first 10 data chars is data, not a separator
    assert deps._decode_dashed("abc-defghi-jklmnopqrs") == "abc-defghijklmnopqrs"


def test_decode_dashed_roundtrip_jwt():
    tok = create_share_token(1, 1, "stream", "cams", 1, None, 60)
    dashed = "-".join(tok[i:i + 10] for i in range(0, len(tok), 10))
    assert deps._decode_dashed(dashed) == tok


def test_expired_access_rejected_by_dep():
    now = int(time.time())
    tok = jwt.encode({"uid": 1, "sid": 1, "role": "user", "iat": now - 100, "exp": now - 1},
                     settings.JWT_SECRET_ACCESS, algorithm="HS256")
    with pytest.raises(AppError):
        deps.require_access(tok)
