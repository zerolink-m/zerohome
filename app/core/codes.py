"""cursedapi v1.1 response codes (docs/cursedapi/docs/codes.md).

A code is a three-digit STRING "XXX". It maps to an HTTP status and a fixed
English detail text (`code_detail` in the envelope). Use the constants, never
literals. Add project-specific codes only
inside the free custom ranges reserved by the spec:
  020-029 success, 036-039 redirect, 061-080 (403), 087-095 (404),
  176-200 (4xx), 216-220 (5xx), 229-255 (any).

Legacy aliases (BAD_REQUEST, UNAUTHORIZED, ...) at the bottom keep old router
imports working during the router migration. See docs/migration_codes.md.
"""
from __future__ import annotations

SUCCESS = "000"
CREATED = "001"
PARTIALLY_CREATED = "002"
EDITED = "003"
PARTIALLY_EDITED = "004"
NOT_CHANGED = "005"
ACCEPTED = "006"
DELETED = "007"
PARTIALLY_DELETED = "008"
AUTHORIZED = "010"
STREAMING = "011"
EMPTY_RESULT = "012"
CACHED = "013"
ALREADY_IN_STATE = "014"
FILE_UPLOADED = "015"
EMAIL_SENT = "016"
PASSWORD_CHANGED = "017"
LOGGED_OUT = "018"
TOKEN_REFRESHED = "019"
FRONTEND_UPDATE_AVAILABLE = "020"
MOVED_PERMANENTLY = "030"
MOVED_TEMPORARILY = "031"
SEE_OTHER = "032"
NOT_MODIFIED = "033"
TEMPORARY_REDIRECT = "034"
PERMANENT_REDIRECT = "035"
BAD_REQUEST = "040"
FIELDS_NOT_EDITABLE = "041"
TOKEN_ERROR = "042"
TOKEN_EXPIRED = "043"
TOKEN_BAD_SIGNATURE = "044"
TOKEN_BAD_ALGORITHM = "045"
TOKEN_MISSING = "046"
PAYMENT_REQUIRED = "047"
FORBIDDEN = "048"
FORBIDDEN_RETRIEVAL = "049"
FORBIDDEN_ROLE = "050"
FORBIDDEN_RESOURCE = "051"
ACTOR_BLOCKED = "052"
YOU_BLOCKED = "053"
EMAIL_NOT_CONFIRMED = "054"
ACCOUNT_NOT_ACTIVATED = "055"
SUBSCRIPTION_REQUIRED = "056"
PLAN_RATE_LIMIT_EXCEEDED = "057"
READ_ONLY = "058"
SELF_EDIT_FORBIDDEN = "059"
IP_BLOCKED = "060"
TUYA_LOCAL_NOT_ALLOWED = "061"
RECORDING_NOT_ALLOWED = "062"
FRONTEND_UPDATE_NEEDED = "063"
NOT_FOUND = "081"
OBJECT_NOT_FOUND = "082"
ROUTE_NOT_FOUND = "083"
FILE_NOT_FOUND = "084"
FIELD_NOT_FOUND = "085"
ENDPOINT_NOT_FOUND = "086"
METHOD_NOT_ALLOWED = "096"
NOT_ACCEPTABLE = "097"
REQUEST_TIMEOUT = "098"
CONFLICT = "099"
FIELD_EXISTS = "100"
HAS_CHILDREN = "101"
GONE = "102"
LENGTH_REQUIRED = "103"
PRECONDITION_FAILED = "104"
PAYLOAD_TOO_LARGE = "105"
URI_TOO_LONG = "106"
UNSUPPORTED_BODY_FORMAT = "107"
RANGE_NOT_SATISFIABLE = "108"
VALIDATION_ERROR = "109"
LOCKED = "110"
LOCKED_TO_EDIT = "111"
LOCKED_TO_DELETE = "112"
PRECONDITION_REQUIRED = "113"
RATE_LIMITED = "114"
RATE_LIMITED_OBJECT = "115"
LEGAL_REASONS = "116"
INVALID_JSON = "117"
INVALID_QUERY = "118"
FIELD_REQUIRED = "119"
FIELD_TYPE_MISMATCH = "120"
EMPTY_BODY = "121"
FILE_TOO_LARGE = "122"
UNSUPPORTED_FILE_TYPE = "123"
INVALID_FILE_CONTENT = "124"
INVALID_EMAIL = "125"
WEAK_PASSWORD = "126"
PASSWORDS_MISMATCH = "127"
INVALID_DATE = "128"
INVALID_PHONE = "129"
INVALID_URL = "130"
OUT_OF_RANGE = "131"
TOO_MANY_FIELDS = "132"
DUPLICATE_VALUES = "133"
CAPTCHA_FAILED = "134"
OTP_INVALID = "135"
OTP_EXPIRED = "136"
OTP_TOO_MANY_ATTEMPTS = "137"
BAD_CREDENTIALS = "138"
EMAIL_TAKEN = "139"
USERNAME_TAKEN = "140"
OLD_PASSWORD_INVALID = "141"
SELF_ACTION_FORBIDDEN = "142"
INVALID_SORT_FIELD = "143"
INVALID_SEARCH_COLUMN = "144"
INVALID_LIMIT_OFFSET = "145"
BULK_LIMIT_EXCEEDED = "146"
NOTHING_TO_UPDATE = "147"
NOTHING_TO_DELETE = "148"
TOKEN_FORMAT_INVALID = "149"
TOKEN_REVOKED = "150"
REFRESH_TOKEN_MISSING = "151"
INVALID_LOCALE = "152"
UNSUPPORTED_MEDIA_TYPE = "153"
REQUEST_CANCELLED = "154"
EDITING_BY_OTHER = "155"
STALE_DATA = "156"
REFERENCED = "157"
IN_PROGRESS = "158"
DUPLICATE_REQUEST = "159"
QUERY_REQUIRED = "160"
FIELD_TOO_LONG = "161"
FIELD_TOO_SHORT = "162"
FIELD_FORBIDDEN_CHARS = "163"
INVALID_ENUM = "164"
FIELD_IMMUTABLE = "165"
TOO_MANY_DECIMALS = "166"
NEGATIVE_NOT_ALLOWED = "167"
EMPTY_NOT_ALLOWED = "168"
INVALID_NESTED = "169"
UNKNOWN_FIELD = "170"
LOCKED_BY_ADMIN = "171"
LOGIN_RATE_LIMITED = "172"
ENDPOINT_RATE_LIMITED = "173"
UPLOAD_RATE_LIMITED = "174"
REGION_BLOCKED = "175"
PTZ_NOT_CONFIGURED = "176"
RECORDING_NOT_CONFIGURED = "177"
INVITATION_INVALID = "178"
PUSH_INVALID = "179"
SHARE_INVALID = "180"
FUNCTION_NOT_SUPPORTED = "181"
FUNCTION_DISABLED = "182"
LIMIT_TOO_LARGE = "183"
EXPAND_NOT_SUPPORTED = "184"
STREAM_INTERRUPTED = "185"
FUNCTIONS_CONFLICT = "186"
BULK_RATE_LIMITED = "187"
SEARCH_EMPTY_POSITION = "188"
SEARCH_LENGTH_MISMATCH = "189"
CONNECTION_INVALID = "190"
SERVER_ERROR = "201"
DATABASE_ERROR = "202"
FILE_STORAGE_ERROR = "203"
EMAIL_SERVICE_ERROR = "204"
CACHE_ERROR = "205"
MIGRATION_IN_PROGRESS = "206"
BULK_NOT_IMPLEMENTED = "207"
SEARCH_NOT_IMPLEMENTED = "208"
PAYMENT_GATEWAY_ERROR = "209"
AUTH_PROVIDER_ERROR = "210"
MAINTENANCE = "211"
DATABASE_STARTING = "212"
FILE_SERVICE_TIMEOUT = "213"
EXPAND_NOT_IMPLEMENTED = "215"
NOT_IMPLEMENTED = "221"
BAD_GATEWAY = "222"
EXTERNAL_API_UNAVAILABLE = "223"
EXTERNAL_BAD_RESPONSE = "224"
SERVICE_UNAVAILABLE = "225"
SERVICE_OVERLOADED = "226"
GATEWAY_TIMEOUT = "227"
EXTERNAL_API_TIMEOUT = "228"

# code -> (http status, detail)
CODES: dict[str, tuple[int, str]] = {
    SUCCESS: (200, 'Success.'),
    CREATED: (201, 'Created successfully.'),
    PARTIALLY_CREATED: (201, 'Partially created successfully.'),
    EDITED: (200, 'Edited successfully.'),
    PARTIALLY_EDITED: (200, 'Partially edited successfully.'),
    NOT_CHANGED: (200, 'Not changed.'),
    ACCEPTED: (202, 'Accepted for work.'),
    DELETED: (200, 'Deleted successfully.'),
    PARTIALLY_DELETED: (200, 'Partially deleted successfully.'),
    AUTHORIZED: (200, 'Authorized successfully.'),
    STREAMING: (200, 'Streaming response.'),
    EMPTY_RESULT: (200, 'Empty result. The request is valid, but no records were found.'),
    CACHED: (200, 'Cached response.'),
    ALREADY_IN_STATE: (200, 'The object is already in the requested state.'),
    FILE_UPLOADED: (200, 'File uploaded successfully.'),
    EMAIL_SENT: (200, 'Email sent successfully.'),
    PASSWORD_CHANGED: (200, 'Password changed successfully.'),
    LOGGED_OUT: (200, 'Logged out successfully.'),
    TOKEN_REFRESHED: (200, 'Token refreshed successfully.'),
    FRONTEND_UPDATE_AVAILABLE: (200, 'A frontend update is available.'),
    MOVED_PERMANENTLY: (301, 'Moved permanently.'),
    MOVED_TEMPORARILY: (302, 'Moved temporarily.'),
    SEE_OTHER: (303, 'See other.'),
    NOT_MODIFIED: (304, 'File not modified.'),
    TEMPORARY_REDIRECT: (307, 'Temporarily relocated while retaining the method.'),
    PERMANENT_REDIRECT: (308, 'Permanently relocated while retaining the method.'),
    BAD_REQUEST: (400, 'Invalid request.'),
    FIELDS_NOT_EDITABLE: (400, 'This fields cannot edit for one or more objects.'),
    TOKEN_ERROR: (401, 'Token error.'),
    TOKEN_EXPIRED: (401, 'The token has expired.'),
    TOKEN_BAD_SIGNATURE: (401, 'The token is signed with an incorrect key.'),
    TOKEN_BAD_ALGORITHM: (401, 'The token has an invalid algorithm.'),
    TOKEN_MISSING: (401, 'No header or query containing the token was found. Authorization is not possible.'),
    PAYMENT_REQUIRED: (402, 'Payment required.'),
    FORBIDDEN: (403, 'Forbidden.'),
    FORBIDDEN_RETRIEVAL: (403, 'This resource is not available for retrieval. Forbidden.'),
    FORBIDDEN_ROLE: (403, 'Forbidden due to an incorrect role.'),
    FORBIDDEN_RESOURCE: (403, 'Access to this resource is denied for this object.'),
    ACTOR_BLOCKED: (403, 'Actor is blocked.'),
    YOU_BLOCKED: (403, 'You are blocked.'),
    EMAIL_NOT_CONFIRMED: (403, 'Email is not confirmed.'),
    ACCOUNT_NOT_ACTIVATED: (403, 'The account is not activated.'),
    SUBSCRIPTION_REQUIRED: (403, 'A subscription is required to access this resource.'),
    PLAN_RATE_LIMIT_EXCEEDED: (403, 'The rate limit for the current plan has been exceeded.'),
    READ_ONLY: (403, 'The object is read-only.'),
    SELF_EDIT_FORBIDDEN: (403, 'Editing your own account this way is forbidden.'),
    IP_BLOCKED: (403, 'Your IP address is blocked.'),
    TUYA_LOCAL_NOT_ALLOWED: (403, 'Local Tuya control is not allowed for this device.'),
    RECORDING_NOT_ALLOWED: (403, 'Recording is not allowed for this camera.'),
    FRONTEND_UPDATE_NEEDED: (403, 'The frontend version is outdated: update required.'),
    NOT_FOUND: (404, 'Not found.'),
    OBJECT_NOT_FOUND: (404, 'Object not found.'),
    ROUTE_NOT_FOUND: (404, 'Route not found.'),
    FILE_NOT_FOUND: (404, 'File not found.'),
    FIELD_NOT_FOUND: (404, 'Field not found.'),
    ENDPOINT_NOT_FOUND: (404, 'Endpoint not found.'),
    METHOD_NOT_ALLOWED: (405, 'The method is not permitted for one or more objects.'),
    NOT_ACCEPTABLE: (406, 'This response format cannot be returned.'),
    REQUEST_TIMEOUT: (408, 'The request from the user timed out.'),
    CONFLICT: (409, 'Conflict.'),
    FIELD_EXISTS: (409, 'The field already exists for this object or one of the objects.'),
    HAS_CHILDREN: (409, 'The object has child objects.'),
    GONE: (410, 'The object is unavailable or has been permanently deleted.'),
    LENGTH_REQUIRED: (411, 'Length Required.'),
    PRECONDITION_FAILED: (412, 'Precondition Failed.'),
    PAYLOAD_TOO_LARGE: (413, 'The request field contains extra elements, or the total request size has been exceeded.'),
    URI_TOO_LONG: (414, 'URI Too Long.'),
    UNSUPPORTED_BODY_FORMAT: (415, 'Unsupported body format.'),
    RANGE_NOT_SATISFIABLE: (416, 'Range not satisfiable.'),
    VALIDATION_ERROR: (422, 'Validation error.'),
    LOCKED: (423, 'Locked.'),
    LOCKED_TO_EDIT: (423, 'Object locked to edit.'),
    LOCKED_TO_DELETE: (423, 'Object locked to delete.'),
    PRECONDITION_REQUIRED: (428, 'Precondition required.'),
    RATE_LIMITED: (429, 'Too many requests.'),
    RATE_LIMITED_OBJECT: (429, 'Too many requests for object.'),
    LEGAL_REASONS: (451, 'Unavailable for legal reasons.'),
    INVALID_JSON: (400, 'Invalid JSON body.'),
    INVALID_QUERY: (400, 'Invalid query parameters.'),
    FIELD_REQUIRED: (400, 'A required field is missing.'),
    FIELD_TYPE_MISMATCH: (400, 'Field type mismatch.'),
    EMPTY_BODY: (400, 'The request body is empty.'),
    FILE_TOO_LARGE: (400, 'The file is too large.'),
    UNSUPPORTED_FILE_TYPE: (400, 'Unsupported file type.'),
    INVALID_FILE_CONTENT: (400, 'Invalid file content.'),
    INVALID_EMAIL: (400, 'Invalid email format.'),
    WEAK_PASSWORD: (400, 'The password is too weak.'),
    PASSWORDS_MISMATCH: (400, 'Passwords do not match.'),
    INVALID_DATE: (400, 'Invalid date format.'),
    INVALID_PHONE: (400, 'Invalid phone number format.'),
    INVALID_URL: (400, 'Invalid URL format.'),
    OUT_OF_RANGE: (400, 'The field value is out of range.'),
    TOO_MANY_FIELDS: (400, 'Too many fields in the request.'),
    DUPLICATE_VALUES: (400, 'Duplicate values in the request.'),
    CAPTCHA_FAILED: (400, 'Captcha verification failed.'),
    OTP_INVALID: (400, 'Invalid one-time code (OTP).'),
    OTP_EXPIRED: (400, 'The one-time code (OTP) has expired.'),
    OTP_TOO_MANY_ATTEMPTS: (400, 'Too many one-time code (OTP) attempts.'),
    BAD_CREDENTIALS: (400, 'Invalid credentials.'),
    EMAIL_TAKEN: (400, 'An account with this email already exists.'),
    USERNAME_TAKEN: (400, 'An account with this username already exists.'),
    OLD_PASSWORD_INVALID: (400, 'The old password is invalid.'),
    SELF_ACTION_FORBIDDEN: (400, 'You cannot perform this action on yourself.'),
    INVALID_SORT_FIELD: (400, 'Invalid sort field.'),
    INVALID_SEARCH_COLUMN: (400, 'Invalid search column.'),
    INVALID_LIMIT_OFFSET: (400, 'Invalid limit or offset value.'),
    BULK_LIMIT_EXCEEDED: (400, 'The bulk operation size limit has been exceeded.'),
    NOTHING_TO_UPDATE: (400, 'There is nothing to update.'),
    NOTHING_TO_DELETE: (400, 'There is nothing to delete.'),
    TOKEN_FORMAT_INVALID: (400, 'Invalid token format.'),
    TOKEN_REVOKED: (400, 'The token has been revoked.'),
    REFRESH_TOKEN_MISSING: (400, 'The refresh token is missing.'),
    INVALID_LOCALE: (400, 'Invalid locale or language.'),
    UNSUPPORTED_MEDIA_TYPE: (400, 'Unsupported media type for this endpoint.'),
    REQUEST_CANCELLED: (400, 'The request was cancelled by the client.'),
    EDITING_BY_OTHER: (409, 'The object is being edited by another user.'),
    STALE_DATA: (409, 'Stale data: the object has changed since it was last fetched.'),
    REFERENCED: (409, 'The object cannot be deleted because it is referenced by other objects.'),
    IN_PROGRESS: (409, 'The operation is already in progress.'),
    DUPLICATE_REQUEST: (409, 'Duplicate request (idempotency conflict).'),
    QUERY_REQUIRED: (422, 'A required query parameter is missing.'),
    FIELD_TOO_LONG: (422, 'The field value is too long.'),
    FIELD_TOO_SHORT: (422, 'The field value is too short.'),
    FIELD_FORBIDDEN_CHARS: (422, 'The field contains forbidden characters.'),
    INVALID_ENUM: (422, 'Invalid enum value.'),
    FIELD_IMMUTABLE: (422, 'The field is immutable.'),
    TOO_MANY_DECIMALS: (422, 'Too many decimal places.'),
    NEGATIVE_NOT_ALLOWED: (422, 'Negative values are not allowed.'),
    EMPTY_NOT_ALLOWED: (422, 'Empty values are not allowed.'),
    INVALID_NESTED: (422, 'Invalid nested object.'),
    UNKNOWN_FIELD: (422, 'Unknown field in the request.'),
    LOCKED_BY_ADMIN: (423, 'The object is locked by an administrator.'),
    LOGIN_RATE_LIMITED: (429, 'Too many login attempts.'),
    ENDPOINT_RATE_LIMITED: (429, 'Too many requests to this endpoint.'),
    UPLOAD_RATE_LIMITED: (429, 'The upload rate limit has been exceeded.'),
    REGION_BLOCKED: (451, 'The content is not available in your region.'),
    PTZ_NOT_CONFIGURED: (409, 'PTZ control is not configured for this camera.'),
    RECORDING_NOT_CONFIGURED: (409, 'Recording is not configured for this camera.'),
    INVITATION_INVALID: (400, 'The invitation is invalid.'),
    PUSH_INVALID: (400, 'Invalid push subscription or settings.'),
    SHARE_INVALID: (400, 'The share link is invalid.'),
    FUNCTION_NOT_SUPPORTED: (400, 'The requested function is not supported by this API server. See the OPTIONS response (cursed-supported-functions).'),
    FUNCTION_DISABLED: (400, 'The requested function is disabled on this API server.'),
    LIMIT_TOO_LARGE: (400, 'Limit value is above the maximum allowed by this API server.'),
    EXPAND_NOT_SUPPORTED: (400, 'Expansion is not supported for one or more requested relations.'),
    STREAM_INTERRUPTED: (400, 'The stream was interrupted before completion.'),
    FUNCTIONS_CONFLICT: (400, 'The request combines functions that cannot be used together.'),
    BULK_RATE_LIMITED: (429, 'The bulk request rate limit has been exceeded.'),
    SEARCH_EMPTY_POSITION: (422, 'A multi search list contains an empty position.'),
    SEARCH_LENGTH_MISMATCH: (422, 'The search values, columns and methods lists have different lengths.'),
    CONNECTION_INVALID: (400, 'The connection is invalid or its parameters are wrong.'),
    SERVER_ERROR: (500, 'Internal server error.'),
    DATABASE_ERROR: (500, 'Database error.'),
    FILE_STORAGE_ERROR: (500, 'File storage error.'),
    EMAIL_SERVICE_ERROR: (500, 'Email service error.'),
    CACHE_ERROR: (500, 'Cache error.'),
    MIGRATION_IN_PROGRESS: (500, 'A database migration is in progress.'),
    BULK_NOT_IMPLEMENTED: (501, 'The bulk operation is not implemented.'),
    SEARCH_NOT_IMPLEMENTED: (501, 'Search is not implemented.'),
    PAYMENT_GATEWAY_ERROR: (502, 'Payment gateway error.'),
    AUTH_PROVIDER_ERROR: (502, 'External auth provider error.'),
    MAINTENANCE: (503, 'The service is under maintenance.'),
    DATABASE_STARTING: (503, 'The database is starting up.'),
    FILE_SERVICE_TIMEOUT: (504, 'The external file service request timed out.'),
    EXPAND_NOT_IMPLEMENTED: (501, 'The expansion feature is not implemented on this server.'),
    NOT_IMPLEMENTED: (501, 'Function not implemented.'),
    BAD_GATEWAY: (502, 'Bad gateway.'),
    EXTERNAL_API_UNAVAILABLE: (502, 'The external API is unavailable.'),
    EXTERNAL_BAD_RESPONSE: (502, 'The external service returned an unexpected response format.'),
    SERVICE_UNAVAILABLE: (503, 'The service is unavailable.'),
    SERVICE_OVERLOADED: (503, 'The service is under load.'),
    GATEWAY_TIMEOUT: (504, 'Gateway timeout.'),
    EXTERNAL_API_TIMEOUT: (504, 'The external API request timed out.'),
}

# Custom ranges: code range -> (http status, generic detail). Used for codes that
# are reserved by the spec but not (yet) registered in CODES.
_RANGES: tuple[tuple[int, int, int, str], ...] = (
    (20, 29, 200, "Success."),
    (30, 39, 302, "Redirect."),
    (61, 80, 403, "Forbidden."),
    (87, 95, 404, "Not found."),
    (176, 200, 400, "Invalid request."),
    (214, 220, 500, "Internal server error."),
    (229, 255, 500, "Error."),
)

HTTP_STATUS_BY_CODE: dict[str, int] = {c: v[0] for c, v in CODES.items()}


def _norm(code: str | int) -> str:
    return code if isinstance(code, str) else f"{code:03d}"


def _range_of(code: str) -> tuple[int, str] | None:
    if not code.isdigit():
        return None
    n = int(code)
    for lo, hi, st, det in _RANGES:
        if lo <= n <= hi:
            return st, det
    return None


def is_known(code: str | int) -> bool:
    code = _norm(code)
    return code in CODES or _range_of(code) is not None


def http_status(code: str | int) -> int:
    """HTTP status for a cursedapi code. Unknown codes -> 500."""
    code = _norm(code)
    hit = CODES.get(code)
    if hit is not None:
        return hit[0]
    rng = _range_of(code)
    return rng[0] if rng else 500


def code_detail(code: str | int) -> str:
    """Fixed English detail text of a code (envelope `code_detail`)."""
    code = _norm(code)
    hit = CODES.get(code)
    if hit is not None:
        return hit[1]
    rng = _range_of(code)
    return rng[1] if rng else "Internal server error."


def is_success(code: str | int) -> bool:
    return http_status(code) < 300
