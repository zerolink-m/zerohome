"""Tuya IoT Cloud client package (formerly top-level tuya_client.py)."""

from app.services.tuya.constants import (  # noqa: F401
    TUYA_ENDPOINTS,
    _EMPTY_BODY_SHA256,
    _DEFAULT_TIMEOUT,
    _TOKEN_REFRESH_LEEWAY_S,
    _RETRYABLE_CODES,
    _TOKEN_INVALID_CODES,
    _MAX_RETRIES,
    _CONFIRM_BACKOFF_MS,
    _LOGGER,
)

from app.services.tuya.errors import (  # noqa: F401
    TuyaError,
    TuyaAuthError,
    TuyaPermissionError,
    TuyaRateLimitError,
    TuyaDeviceOfflineError,
    TuyaDeviceNotFoundError,
    TuyaValueError,
    TuyaAPIError,
    _ERROR_CODE_MAP,
    _exception_for,
)

from app.services.tuya.models import (  # noqa: F401
    _TuyaModel,
    Device,
    DPValue,
    FunctionValues,
    Function,
    Specifications,
    HomeInfo,
    LogEntry,
    WebRTCConfig,
    StreamURL,
)

from app.services.tuya.utils import (  # noqa: F401
    _TokenState,
    hash_password,
    _mask,
    _canonical_query,
    _serialize_body,
    _sha256_hex,
    _hmac_sha256_upper,
)

from app.services.tuya.client import (  # noqa: F401
    TuyaCloudClient,
    _query_for_httpx,
    _backoff_delay,
    _parse_functions,
    _parse_specifications,
)

from app.services.tuya.pulsar import (  # noqa: F401
    _PULSAR_AVAILABLE,
    _CRYPTO_AVAILABLE,
    _PULSAR_BROKERS,
    _unpad_pkcs7,
    TuyaPulsarConsumer,
)
