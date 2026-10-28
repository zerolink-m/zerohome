import pytest

from app.core import codes


def test_all_codes_are_three_digit_strings():
    for code, (st, detail) in codes.CODES.items():
        assert len(code) == 3 and code.isdigit()
        assert 200 <= st <= 599 and detail


@pytest.mark.parametrize("code,status", [
    ("000", 200), ("001", 201), ("006", 202), ("042", 401), ("048", 403), ("081", 404), ("096", 405),
    ("109", 422), ("114", 429), ("201", 500), ("221", 501), ("188", 422), ("181", 400), ("215", 501),
    ("172", 429), ("187", 429), ("075", 403), ("090", 404), ("190", 400), ("218", 500),
])
def test_http_status(code, status):
    assert codes.http_status(code) == status


def test_detail_texts():
    assert codes.code_detail("000") == "Success."
    assert codes.code_detail("109") == "Validation error."
    assert codes.code_detail("046").startswith("No header or query")


def test_custom_codes_in_reserved_ranges():
    custom = {"FRONTEND_UPDATE_AVAILABLE": (20, 29), "TUYA_LOCAL_NOT_ALLOWED": (61, 80), "RECORDING_NOT_ALLOWED": (61, 80),
              "FRONTEND_UPDATE_NEEDED": (61, 80), "PTZ_NOT_CONFIGURED": (176, 200), "RECORDING_NOT_CONFIGURED": (176, 200),
              "INVITATION_INVALID": (176, 200), "PUSH_INVALID": (176, 200), "SHARE_INVALID": (176, 200),
              "CONNECTION_INVALID": (176, 200)}
    for name, (lo, hi) in custom.items():
        assert lo <= int(getattr(codes, name)) <= hi, name


def test_legacy_aliases():
    assert codes.TOKEN_ERROR == "042" and codes.EMAIL_TAKEN == "139" and codes.LOCKED == "110"
