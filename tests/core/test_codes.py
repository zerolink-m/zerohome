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


def test_service_codes_are_unique_and_keep_published_assignments():
    expected = {
        "FRONTEND_UPDATE_AVAILABLE": ("384", 200),
        "TUYA_LOCAL_NOT_ALLOWED": ("385", 403),
        "RECORDING_NOT_ALLOWED": ("386", 403),
        "FRONTEND_UPDATE_NEEDED": ("387", 403),
        "PTZ_NOT_CONFIGURED": ("388", 409),
        "RECORDING_NOT_CONFIGURED": ("389", 409),
        "INVITATION_INVALID": ("390", 400),
        "PUSH_INVALID": ("391", 400),
        "SHARE_INVALID": ("392", 400),
        "CONNECTION_INVALID": ("393", 400),
        "PARTIAL_CONTENT": ("394", 206),
        "NO_CONTENT": ("395", 204),
        "INVITATION_ALREADY_EXISTS": ("396", 400),
        "INVITATION_OWNER_FORBIDDEN": ("397", 400),
        "CONNECTION_DISABLED": ("398", 400),
        "IMPORTED_CAMERA_READ_ONLY": ("399", 400),
        "IMPORTED_CAMERA_DELETE_VIA_CONNECTION": ("400", 400),
        "ONVIF_CAMERA_AUTH_REJECTED": ("401", 409),
        "ONVIF_CAMERA_TIMEOUT": ("402", 409),
        "CLOUD_CAMERA_REQUIRES_HLS": ("403", 400),
    }
    names = [entry[0] for entry in codes._SERVICE_CODES]
    numbers = [entry[1] for entry in codes._SERVICE_CODES]
    assert len(names) == len(set(names)) and len(numbers) == len(set(numbers))
    standard = get_profile("1.2").new_codes()
    assert all(getattr(standard, name, None) is None for name in names)
    assert all(standard.by_number(number) is None for number in numbers)
    codes.register_cursed_codes(standard)
    for name, (number, status) in expected.items():
        entry = getattr(standard, name)
        assert getattr(codes, name) == entry.code == number
        assert entry.http_status == codes.http_status(number) == status
        assert entry.code_detail == codes.code_detail(number) and entry.code_detail


@pytest.mark.parametrize("number,detail", [
    ("396", "The recipient is already invited to this device."),
    ("397", "The device owner cannot be invited to the device."),
    ("398", "The connection is disabled."),
    ("399", "The imported camera is read-only."),
    ("400", "The imported camera can only be deleted with its connection."),
    ("401", "The camera rejected ONVIF authentication or locked the account."),
    ("402", "The camera did not respond to the ONVIF request in time."),
    ("403", "Use HLS to stream this cloud camera."),
])
def test_new_service_code_details(number, detail):
    assert codes.code_detail(number) == detail


@pytest.mark.parametrize("duplicate", [
    ("FRONTEND_UPDATE_AVAILABLE", "511", 200, "Duplicate name."),
    ("UNRELATED_CODE", "384", 200, "Duplicate number."),
])
def test_duplicate_service_entries_do_not_partially_register(monkeypatch, duplicate):
    registry = get_profile("1.2").new_codes()
    monkeypatch.setattr(codes, "_SERVICE_CODES", (*codes._SERVICE_CODES, duplicate))
    with pytest.raises(ValueError, match="conflicts"):
        codes.register_cursed_codes(registry)
    assert registry.by_number("384") is None
