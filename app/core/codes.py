"""cursedapi v1.2 standard and ZeroHome service response codes.

Standard codes occupy the specification-defined 000–383 space. Service-specific
codes are registered in 384–511. Legacy symbol names remain stable for routers;
their values now follow v1.2.
"""
from __future__ import annotations

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

# Every assigned standard code from the v1.2 table.
CODES: dict[str, tuple[int, str]] = {
    '000': (200, 'Success.'),
    '002': (200, 'Edited successfully.'),
    '004': (200, 'Partially edited successfully.'),
    '007': (200, 'Not changed.'),
    '009': (200, 'Deleted successfully.'),
    '012': (200, 'Partially deleted successfully.'),
    '014': (200, 'Authorized successfully.'),
    '017': (200, 'Streaming response.'),
    '019': (200, 'Empty result.'),
    '022': (200, 'Cached response.'),
    '024': (200, 'The object is already in the requested state.'),
    '026': (200, 'File uploaded successfully.'),
    '029': (200, 'Email sent successfully.'),
    '031': (200, 'Password changed successfully.'),
    '034': (200, 'Logged out successfully.'),
    '036': (200, 'Token refreshed successfully.'),
    '039': (201, 'Created successfully.'),
    '041': (201, 'Partially created successfully.'),
    '044': (202, 'Accepted for work.'),
    '045': (301, 'Moved permanently.'),
    '047': (302, 'Moved temporarily.'),
    '050': (303, 'See other.'),
    '052': (304, 'File not modified.'),
    '055': (307, 'Temporarily relocated while retaining the method.'),
    '058': (308, 'Permanently relocated while retaining the method.'),
    '059': (400, 'Invalid request.'),
    '061': (400, 'These fields cannot be edited for one or more objects.'),
    '063': (400, 'Invalid JSON body.'),
    '066': (400, 'Invalid query parameters.'),
    '068': (400, 'A required field is missing.'),
    '070': (400, 'Field type mismatch.'),
    '073': (400, 'The request body is empty.'),
    '075': (400, 'The file is too large.'),
    '077': (400, 'Unsupported file type.'),
    '080': (400, 'Invalid file content.'),
    '082': (400, 'Invalid email format.'),
    '085': (400, 'The password is too weak.'),
    '087': (400, 'Passwords do not match.'),
    '089': (400, 'Invalid date format.'),
    '092': (400, 'Invalid phone number format.'),
    '094': (400, 'Invalid URL format.'),
    '096': (400, 'The field value is out of range.'),
    '099': (400, 'Too many fields in the request.'),
    '101': (400, 'Duplicate values in the request.'),
    '103': (400, 'Captcha verification failed.'),
    '106': (400, 'Invalid one-time code (OTP).'),
    '108': (400, 'The one-time code (OTP) has expired.'),
    '111': (400, 'Too many one-time code (OTP) attempts.'),
    '113': (400, 'Invalid credentials.'),
    '115': (400, 'An account with this email already exists.'),
    '118': (400, 'An account with this username already exists.'),
    '120': (400, 'The old password is invalid.'),
    '122': (400, 'You cannot perform this action on yourself.'),
    '125': (400, 'Invalid sort field.'),
    '127': (400, 'Invalid search column.'),
    '129': (400, 'Invalid limit or offset value.'),
    '132': (400, 'The bulk operation size limit has been exceeded.'),
    '134': (400, 'There is nothing to update.'),
    '137': (400, 'There is nothing to delete.'),
    '139': (400, 'Invalid token format.'),
    '141': (400, 'The token has been revoked.'),
    '144': (400, 'The refresh token is missing.'),
    '146': (400, 'Invalid locale or language.'),
    '148': (400, 'Unsupported media type for this endpoint.'),
    '151': (400, 'The request was cancelled by the client.'),
    '153': (400, 'The requested function is not supported by this API server.'),
    '155': (400, 'The requested function is disabled on this API server.'),
    '158': (400, 'Limit value is above the maximum allowed by this API server.'),
    '160': (400, 'Expansion is not supported for one or more requested relations.'),
    '163': (400, 'The stream was interrupted before completion.'),
    '165': (400, 'The request combines functions that cannot be used together.'),
    '167': (401, 'Token error.'),
    '170': (401, 'The token has expired.'),
    '172': (401, 'The token is signed with an incorrect key.'),
    '174': (401, 'The token has an invalid algorithm.'),
    '177': (401, 'No header or query containing the token was found.'),
    '179': (402, 'Payment required.'),
    '181': (403, 'Forbidden.'),
    '184': (403, 'This resource is not available for retrieval. Forbidden.'),
    '186': (403, 'Forbidden due to an incorrect role.'),
    '189': (403, 'Access to this resource is denied for this object.'),
    '191': (403, 'Actor is blocked.'),
    '193': (403, 'You are blocked.'),
    '196': (403, 'Email is not confirmed.'),
    '198': (403, 'The account is not activated.'),
    '200': (403, 'A subscription is required to access this resource.'),
    '203': (403, 'The rate limit for the current plan has been exceeded.'),
    '205': (403, 'The object is read-only.'),
    '208': (403, 'Editing your own account this way is forbidden.'),
    '210': (403, 'Your IP address is blocked.'),
    '212': (404, 'Not found.'),
    '215': (404, 'Object not found.'),
    '217': (404, 'Route not found.'),
    '219': (404, 'File not found.'),
    '222': (404, 'Field not found.'),
    '224': (404, 'Endpoint not found.'),
    '226': (405, 'The method is not permitted for one or more objects.'),
    '229': (406, 'This response format cannot be returned.'),
    '231': (408, 'The request from the user timed out.'),
    '234': (409, 'Conflict.'),
    '236': (409, 'The field already exists for this object or one of the objects.'),
    '238': (409, 'The object has child objects.'),
    '241': (409, 'The object is being edited by another user.'),
    '243': (409, 'Stale data: the object has changed since it was last fetched.'),
    '245': (409, 'The object cannot be deleted because it is referenced by other objects.'),
    '248': (409, 'The operation is already in progress.'),
    '250': (409, 'Duplicate request (idempotency conflict).'),
    '252': (410, 'The object is unavailable or has been permanently deleted.'),
    '255': (411, 'Length Required.'),
    '257': (412, 'Precondition Failed.'),
    '260': (413, 'The request field contains extra elements, or the total request size has been exceeded.'),
    '262': (414, 'URI Too Long.'),
    '264': (415, 'Unsupported body format.'),
    '267': (416, 'Range not satisfiable.'),
    '269': (422, 'Validation error.'),
    '271': (422, 'A required query parameter is missing.'),
    '274': (422, 'The field value is too long.'),
    '276': (422, 'The field value is too short.'),
    '278': (422, 'The field contains forbidden characters.'),
    '281': (422, 'Invalid enum value.'),
    '283': (422, 'The field is immutable.'),
    '286': (422, 'Too many decimal places.'),
    '288': (422, 'Negative values are not allowed.'),
    '290': (422, 'Empty values are not allowed.'),
    '293': (422, 'Invalid nested object.'),
    '295': (422, 'Unknown field in the request.'),
    '297': (422, 'A multi search list contains an empty position.'),
    '300': (422, 'The search values, columns and methods lists have different lengths.'),
    '302': (423, 'Locked.'),
    '304': (423, 'Object locked to edit.'),
    '307': (423, 'Object locked to delete.'),
    '309': (423, 'The object is locked by an administrator.'),
    '312': (428, 'Precondition required.'),
    '314': (429, 'Too many requests.'),
    '316': (429, 'Too many requests for object.'),
    '319': (429, 'Too many login attempts.'),
    '321': (429, 'Too many requests to this endpoint.'),
    '323': (429, 'The upload rate limit has been exceeded.'),
    '326': (429, 'The bulk request rate limit has been exceeded.'),
    '328': (451, 'Unavailable for legal reasons.'),
    '331': (451, 'The content is not available in your region.'),
    '332': (500, 'Internal server error.'),
    '334': (500, 'Database error.'),
    '336': (500, 'File storage error.'),
    '339': (500, 'Email service error.'),
    '341': (500, 'Cache error.'),
    '344': (500, 'A database migration is in progress.'),
    '346': (501, 'The bulk operation is not implemented.'),
    '349': (501, 'Search is not implemented.'),
    '351': (501, 'The expansion feature is not implemented on this server.'),
    '353': (501, 'Function not implemented.'),
    '356': (502, 'Payment gateway error.'),
    '358': (502, 'External auth provider error.'),
    '361': (502, 'Bad gateway.'),
    '363': (502, 'The external API is unavailable.'),
    '366': (502, 'The external service returned an unexpected response format.'),
    '368': (503, 'The service is under maintenance.'),
    '370': (503, 'The database is starting up.'),
    '373': (503, 'The service is unavailable.'),
    '375': (503, 'The service is under load.'),
    '378': (504, 'The external file service request timed out.'),
    '380': (504, 'Gateway timeout.'),
    '383': (504, 'The external API request timed out.'),
    '384': (200, 'A frontend update is available.'),  # FRONTEND_UPDATE_AVAILABLE
    '385': (403, 'Local Tuya control is not allowed for this device.'),  # TUYA_LOCAL_NOT_ALLOWED
    '386': (403, 'Recording is not allowed for this camera.'),  # RECORDING_NOT_ALLOWED
    '387': (403, 'The frontend version is outdated: update required.'),  # FRONTEND_UPDATE_NEEDED
    '388': (409, 'PTZ control is not configured for this camera.'),  # PTZ_NOT_CONFIGURED
    '389': (409, 'Recording is not configured for this camera.'),  # RECORDING_NOT_CONFIGURED
    '390': (400, 'The invitation is invalid.'),  # INVITATION_INVALID
    '391': (400, 'Invalid push subscription or settings.'),  # PUSH_INVALID
    '392': (400, 'The share link is invalid.'),  # SHARE_INVALID
    '393': (400, 'The connection is invalid or its parameters are wrong.'),  # CONNECTION_INVALID
    '394': (206, 'Partial content.'),  # PARTIAL_CONTENT
    '395': (204, 'No content.'),  # NO_CONTENT
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


_HTTP_MAIN_CODE = {
    200: SUCCESS, 201: CREATED, 202: ACCEPTED,
    301: MOVED_PERMANENTLY, 302: MOVED_TEMPORARILY, 303: SEE_OTHER,
    304: NOT_MODIFIED, 307: TEMPORARY_REDIRECT, 308: PERMANENT_REDIRECT,
    400: BAD_REQUEST, 401: TOKEN_ERROR, 402: PAYMENT_REQUIRED, 403: FORBIDDEN,
    404: NOT_FOUND, 405: METHOD_NOT_ALLOWED, 406: NOT_ACCEPTABLE, 408: REQUEST_TIMEOUT,
    409: CONFLICT, 410: GONE, 411: LENGTH_REQUIRED, 412: PRECONDITION_FAILED,
    413: PAYLOAD_TOO_LARGE, 414: URI_TOO_LONG, 415: UNSUPPORTED_BODY_FORMAT,
    416: RANGE_NOT_SATISFIABLE, 422: VALIDATION_ERROR, 423: LOCKED,
    428: PRECONDITION_REQUIRED, 429: RATE_LIMITED, 451: LEGAL_REASONS,
    500: SERVER_ERROR, 501: NOT_IMPLEMENTED, 502: BAD_GATEWAY,
    503: SERVICE_UNAVAILABLE, 504: GATEWAY_TIMEOUT,
}


def code_for_http_status(status: int) -> str:
    """Return the v1.2 main code for an HTTP status, including service statuses."""
    code = _HTTP_MAIN_CODE.get(status)
    if code is not None:
        return code
    return next((c for c, (s, _) in CODES.items() if 384 <= int(c) <= 511 and s == status), SERVER_ERROR)
