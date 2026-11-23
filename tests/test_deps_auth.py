import pytest

from app.core.codes import FORBIDDEN_ROLE, TOKEN_ERROR
from app.core.errors import AppError
from app.core.security import create_session_token, create_share_token, is_opaque_token, new_bearer_token
from app.deps import auth as deps
from tests.conftest import bearer, mint_test_token


def test_bearer_parsing():
    assert deps.get_bearer_token("Bearer abc") == "abc"
    for value in (None, "", "Bearer "):
        with pytest.raises(AppError) as exc:
            deps.get_bearer_token(value)
        assert exc.value.code == "177"
    with pytest.raises(AppError) as exc:
        deps.get_bearer_token("Basic abc")
    assert exc.value.code == TOKEN_ERROR


def test_opaque_format_and_legacy_distinction():
    token = new_bearer_token()
    assert is_opaque_token(token)
    assert not is_opaque_token(create_session_token(1, 1))
    assert not is_opaque_token(create_share_token(1, 1, "stream", "cams", 1, None, 60))


def test_dash_roundtrip_and_header_priority():
    token = new_bearer_token()
    dashed = "-".join(token[i:i + 10] for i in range(0, len(token), 10))
    assert deps._decode_dashed(dashed) == token
    assert deps._query_token_candidates(dashed)[-1] == token
    assert deps.extract_token_flexible(f"Bearer {token}", "junk") == token
    assert deps.extract_token_flexible(None, dashed) == dashed


async def test_database_auth_and_role(db, app, mk_user):
    uid = await mk_user("u@x.io")
    admin = await mk_user("a@x.io", role="admin")
    token, admin_token = await mint_test_token(uid), await mint_test_token(admin, "admin")
    async with db() as s:
        payload = await deps._authenticate(token, s, "pytest-UA/1.0")
        assert payload.uid == uid and payload.role == "user"
        assert (await deps._authenticate(admin_token, s, "pytest-UA/1.0")).role == "admin"
        with pytest.raises(AppError) as exc:
            await deps._authenticate(create_session_token(uid, 1), s, "pytest-UA/1.0")
        assert exc.value.code == TOKEN_ERROR
