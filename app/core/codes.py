"""cursedapi v1.2 standard and ZeroHome service response codes.

Standard codes occupy the specification-defined 000–383 space. Service-specific
codes are registered in 384–511. Legacy symbol names remain stable for routers;
their values now follow v1.2.
"""
from __future__ import annotations

from cursedapi.codes import Code, CodeRegistry
from cursedapi.protocols import get_profile

SUCCESS = '000'
CREATED = '039'
PARTIALLY_CREATED = '041'
EDITED = '002'
PARTIALLY_EDITED = '004'
NOT_CHANGED = '007'
ACCEPTED = '044'
DELETED = '009'
PARTIALLY_DELETED = '012'
AUTHORIZED = '014'
STREAMING = '017'
EMPTY_RESULT = '019'
CACHED = '022'
ALREADY_IN_STATE = '024'
FILE_UPLOADED = '026'
EMAIL_SENT = '029'
PASSWORD_CHANGED = '031'
LOGGED_OUT = '034'
TOKEN_REFRESHED = '036'
FRONTEND_UPDATE_AVAILABLE = '384'
MOVED_PERMANENTLY = '045'
MOVED_TEMPORARILY = '047'
SEE_OTHER = '050'
NOT_MODIFIED = '052'
TEMPORARY_REDIRECT = '055'
PERMANENT_REDIRECT = '058'
BAD_REQUEST = '059'
FIELDS_NOT_EDITABLE = '061'
TOKEN_ERROR = '167'
TOKEN_EXPIRED = '170'
TOKEN_BAD_SIGNATURE = '172'
TOKEN_BAD_ALGORITHM = '174'
TOKEN_MISSING = '177'
PAYMENT_REQUIRED = '179'
FORBIDDEN = '181'
FORBIDDEN_RETRIEVAL = '184'
FORBIDDEN_ROLE = '186'
FORBIDDEN_RESOURCE = '189'
ACTOR_BLOCKED = '191'
YOU_BLOCKED = '193'
EMAIL_NOT_CONFIRMED = '196'
ACCOUNT_NOT_ACTIVATED = '198'
SUBSCRIPTION_REQUIRED = '200'
PLAN_RATE_LIMIT_EXCEEDED = '203'
READ_ONLY = '205'
SELF_EDIT_FORBIDDEN = '208'
IP_BLOCKED = '210'
TUYA_LOCAL_NOT_ALLOWED = '385'
RECORDING_NOT_ALLOWED = '386'
FRONTEND_UPDATE_NEEDED = '387'
NOT_FOUND = '212'
OBJECT_NOT_FOUND = '215'
ROUTE_NOT_FOUND = '217'
FILE_NOT_FOUND = '219'
FIELD_NOT_FOUND = '222'
ENDPOINT_NOT_FOUND = '224'
METHOD_NOT_ALLOWED = '226'
NOT_ACCEPTABLE = '229'
REQUEST_TIMEOUT = '231'
CONFLICT = '234'
FIELD_EXISTS = '236'
HAS_CHILDREN = '238'
GONE = '252'
LENGTH_REQUIRED = '255'
PRECONDITION_FAILED = '257'
PAYLOAD_TOO_LARGE = '260'
URI_TOO_LONG = '262'
UNSUPPORTED_BODY_FORMAT = '264'
RANGE_NOT_SATISFIABLE = '267'
VALIDATION_ERROR = '269'
LOCKED = '302'
LOCKED_TO_EDIT = '304'
LOCKED_TO_DELETE = '307'
PRECONDITION_REQUIRED = '312'
RATE_LIMITED = '314'
RATE_LIMITED_OBJECT = '316'
LEGAL_REASONS = '328'
INVALID_JSON = '063'
INVALID_QUERY = '066'
FIELD_REQUIRED = '068'
FIELD_TYPE_MISMATCH = '070'
EMPTY_BODY = '073'
FILE_TOO_LARGE = '075'
UNSUPPORTED_FILE_TYPE = '077'
INVALID_FILE_CONTENT = '080'
INVALID_EMAIL = '082'
WEAK_PASSWORD = '085'
PASSWORDS_MISMATCH = '087'
INVALID_DATE = '089'
INVALID_PHONE = '092'
INVALID_URL = '094'
OUT_OF_RANGE = '096'
TOO_MANY_FIELDS = '099'
DUPLICATE_VALUES = '101'
CAPTCHA_FAILED = '103'
OTP_INVALID = '106'
OTP_EXPIRED = '108'
OTP_TOO_MANY_ATTEMPTS = '111'
BAD_CREDENTIALS = '113'
EMAIL_TAKEN = '115'
USERNAME_TAKEN = '118'
OLD_PASSWORD_INVALID = '120'
SELF_ACTION_FORBIDDEN = '122'
INVALID_SORT_FIELD = '125'
INVALID_SEARCH_COLUMN = '127'
INVALID_LIMIT_OFFSET = '129'
BULK_LIMIT_EXCEEDED = '132'
NOTHING_TO_UPDATE = '134'
NOTHING_TO_DELETE = '137'
TOKEN_FORMAT_INVALID = '139'
TOKEN_REVOKED = '141'
REFRESH_TOKEN_MISSING = '144'
INVALID_LOCALE = '146'
UNSUPPORTED_MEDIA_TYPE = '148'
REQUEST_CANCELLED = '151'
EDITING_BY_OTHER = '241'
STALE_DATA = '243'
REFERENCED = '245'
IN_PROGRESS = '248'
DUPLICATE_REQUEST = '250'
QUERY_REQUIRED = '271'
FIELD_TOO_LONG = '274'
FIELD_TOO_SHORT = '276'
FIELD_FORBIDDEN_CHARS = '278'
INVALID_ENUM = '281'
FIELD_IMMUTABLE = '283'
TOO_MANY_DECIMALS = '286'
NEGATIVE_NOT_ALLOWED = '288'
EMPTY_NOT_ALLOWED = '290'
INVALID_NESTED = '293'
UNKNOWN_FIELD = '295'
LOCKED_BY_ADMIN = '309'
LOGIN_RATE_LIMITED = '319'
ENDPOINT_RATE_LIMITED = '321'
UPLOAD_RATE_LIMITED = '323'
REGION_BLOCKED = '331'
PTZ_NOT_CONFIGURED = '388'
RECORDING_NOT_CONFIGURED = '389'
INVITATION_INVALID = '390'
PUSH_INVALID = '391'
SHARE_INVALID = '392'
FUNCTION_NOT_SUPPORTED = '153'
FUNCTION_DISABLED = '155'
LIMIT_TOO_LARGE = '158'
EXPAND_NOT_SUPPORTED = '160'
STREAM_INTERRUPTED = '163'
FUNCTIONS_CONFLICT = '165'
BULK_RATE_LIMITED = '326'
SEARCH_EMPTY_POSITION = '297'
SEARCH_LENGTH_MISMATCH = '300'
CONNECTION_INVALID = '393'
PARTIAL_CONTENT = '394'
NO_CONTENT = '395'
SERVER_ERROR = '332'
DATABASE_ERROR = '334'
FILE_STORAGE_ERROR = '336'
EMAIL_SERVICE_ERROR = '339'
CACHE_ERROR = '341'
MIGRATION_IN_PROGRESS = '344'
BULK_NOT_IMPLEMENTED = '346'
SEARCH_NOT_IMPLEMENTED = '349'
PAYMENT_GATEWAY_ERROR = '356'
AUTH_PROVIDER_ERROR = '358'
MAINTENANCE = '368'
DATABASE_STARTING = '370'
FILE_SERVICE_TIMEOUT = '378'
EXPAND_NOT_IMPLEMENTED = '351'
NOT_IMPLEMENTED = '353'
BAD_GATEWAY = '361'
EXTERNAL_API_UNAVAILABLE = '363'
EXTERNAL_BAD_RESPONSE = '366'
SERVICE_UNAVAILABLE = '373'
SERVICE_OVERLOADED = '375'
GATEWAY_TIMEOUT = '380'
EXTERNAL_API_TIMEOUT = '383'

# Only ZeroHome extensions are defined here. The protocol owns standard codes.
_SERVICE_CODES = (
    ("FRONTEND_UPDATE_AVAILABLE", FRONTEND_UPDATE_AVAILABLE, 200, "A frontend update is available."),
    ("TUYA_LOCAL_NOT_ALLOWED", TUYA_LOCAL_NOT_ALLOWED, 403, "Local Tuya control is not allowed for this device."),
    ("RECORDING_NOT_ALLOWED", RECORDING_NOT_ALLOWED, 403, "Recording is not allowed for this camera."),
    ("FRONTEND_UPDATE_NEEDED", FRONTEND_UPDATE_NEEDED, 403, "The frontend version is outdated: update required."),
    ("PTZ_NOT_CONFIGURED", PTZ_NOT_CONFIGURED, 409, "PTZ control is not configured for this camera."),
    ("RECORDING_NOT_CONFIGURED", RECORDING_NOT_CONFIGURED, 409, "Recording is not configured for this camera."),
    ("INVITATION_INVALID", INVITATION_INVALID, 400, "The invitation is invalid."),
    ("PUSH_INVALID", PUSH_INVALID, 400, "Invalid push subscription or settings."),
    ("SHARE_INVALID", SHARE_INVALID, 400, "The share link is invalid."),
    ("CONNECTION_INVALID", CONNECTION_INVALID, 400, "The connection is invalid or its parameters are wrong."),
    ("PARTIAL_CONTENT", PARTIAL_CONTENT, 206, "Partial content."),
    ("NO_CONTENT", NO_CONTENT, 204, "No content."),
)


def register_cursed_codes(registry: CodeRegistry) -> None:
    """Register ZeroHome extensions on a new v1.2 registry before route setup."""
    success = registry.by_number(SUCCESS)
    if success is None or success.version != "1.2":
        raise ValueError("ZeroHome codes require a cursedapi 1.2 registry")
    # Check every extension before mutating the registry so collisions cannot
    # leave a partially registered service.
    for name, number, _, _ in _SERVICE_CODES:
        if getattr(registry, name, None) is not None or registry.by_number(number) is not None:
            raise ValueError(f"ZeroHome code {name} ({number}) conflicts with an existing registration")
    for name, number, status, detail in _SERVICE_CODES:
        registry.register(name, number, detail, status)


def cursed_code(registry: CodeRegistry, code: str | int) -> Code:
    """Resolve a ZeroHome code constant to the library's Code object."""
    number = _norm(code)
    registered = registry.by_number(number)
    if registered is None:
        raise ValueError(f"Unknown ZeroHome response code {number!r}")
    return registered


_registry = get_profile("1.2").new_codes()
register_cursed_codes(_registry)
_registry.freeze()
CODES: dict[str, tuple[int, str]] = {
    code.code: (code.http_status, code.code_detail) for code in _registry
}

HTTP_STATUS_BY_CODE: dict[str, int] = {code: item[0] for code, item in CODES.items()}


def _norm(code: str | int) -> str:
    return code if isinstance(code, str) else f"{code:03d}"


def is_known(code: str | int) -> bool:
    return _norm(code) in CODES


def http_status(code: str | int) -> int:
    """HTTP status for a registered standard/service code; unknown codes -> 500."""
    hit = CODES.get(_norm(code))
    return hit[0] if hit is not None else 500


def code_detail(code: str | int) -> str:
    """Specification detail for a registered code."""
    hit = CODES.get(_norm(code))
    return hit[1] if hit is not None else "Internal server error."


def is_success(code: str | int) -> bool:
    return http_status(code) < 300


def code_for_http_status(status: int) -> str:
    """Return the v1.2 main code for an HTTP status, including service statuses."""
    standard = _registry.by_http(status)
    if standard is not None:
        return standard.code
    return next(
        (number for _, number, custom_status, _ in _SERVICE_CODES if custom_status == status),
        SERVER_ERROR,
    )
