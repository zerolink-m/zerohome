import pytest
from cursedapi.protocols import get_profile

from app.core import codes


def test_all_codes_are_three_digit_strings():
    for code, (st, detail) in codes.CODES.items():
        assert len(code) == 3 and code.isdigit()
        assert 200 <= st <= 599 and detail


@pytest.mark.parametrize("code,status", [
    ("000", 200), ("039", 201), ("044", 202), ("167", 401), ("181", 403), ("212", 404), ("226", 405),
    ("269", 422), ("314", 429), ("332", 500), ("353", 501), ("297", 422), ("153", 400), ("215", 404),
    ("172", 401), ("326", 429), ("075", 400), ("217", 404), ("393", 400), ("380", 504),
])
def test_http_status(code, status):
    assert codes.http_status(code) == status


def test_detail_texts():
    assert codes.code_detail("000") == "Success."
    assert codes.code_detail("269") == "Validation error."
    assert codes.code_detail("177").startswith("No header or query")


def test_custom_codes_in_reserved_ranges():
    custom = {"FRONTEND_UPDATE_AVAILABLE": (384, 511), "TUYA_LOCAL_NOT_ALLOWED": (384, 511),
              "RECORDING_NOT_ALLOWED": (384, 511), "FRONTEND_UPDATE_NEEDED": (384, 511),
              "PTZ_NOT_CONFIGURED": (384, 511), "RECORDING_NOT_CONFIGURED": (384, 511),
              "INVITATION_INVALID": (384, 511), "PUSH_INVALID": (384, 511), "SHARE_INVALID": (384, 511),
              "CONNECTION_INVALID": (384, 511)}
    for name in custom:
        low, high = custom[name]
        assert low <= int(getattr(codes, name)) <= high, name


def test_legacy_aliases():
    assert codes.TOKEN_ERROR == "167" and codes.EMAIL_TAKEN == "115" and codes.LOCKED == "302"


def test_zero_home_registry_preserves_codes_for_schema_and_responses():
    registry = get_profile("1.2").new_codes()
    codes.register_cursed_codes(registry)
    assert codes.CODES == {entry.code: (entry.http_status, entry.code_detail) for entry in registry}
    assert codes.cursed_code(registry, codes.BAD_REQUEST) is registry.INVALID_REQUEST
    assert codes.cursed_code(registry, codes.PARTIAL_CONTENT) is registry.PARTIAL_CONTENT
    assert codes.code_for_http_status(206) == codes.PARTIAL_CONTENT
    assert codes.code_for_http_status(204) == codes.NO_CONTENT


def test_zero_home_rejects_existing_code_assignment_before_registration():
    registry = get_profile("1.2").new_codes()
    registry.register("UNRELATED_CODE", codes.CONNECTION_INVALID, "Unrelated code.", 400)
    with pytest.raises(ValueError, match="CONNECTION_INVALID"):
        codes.register_cursed_codes(registry)
    assert registry.by_number(codes.FRONTEND_UPDATE_AVAILABLE) is None
    assert registry.by_number(codes.CONNECTION_INVALID) is registry.UNRELATED_CODE
