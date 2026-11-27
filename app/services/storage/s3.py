"""Minimal S3-compatible client (path-style PUT/GET/DELETE/HEAD) signed with SigV4.

boto3/aiobotocore are not project dependencies. This speaks just the object
operations we need, over the httpx the app already uses. Access keys are never logged.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

from app.services.storage.base import validate_key

logger = logging.getLogger(__name__)

# httpx logs request URLs at INFO. Object keys are not secrets, but keep this quiet anyway.
logging.getLogger("httpx").setLevel(logging.WARNING)

_EMPTY_PAYLOAD_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_ALGORITHM = "AWS4-HMAC-SHA256"


class StorageError(RuntimeError):
    """Object store request failed. The message never includes credentials."""


def payload_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def endpoint_host(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RuntimeError("S3_ENDPOINT must be an absolute http(s) URL, for example http://minio:9000")
    if parts.query or parts.fragment or parts.username is not None or parts.password is not None:
        raise RuntimeError("S3_ENDPOINT must not include credentials, a query, or a fragment")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    port = parts.port
    if port is None or (parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80):
        return host
    return f"{host}:{port}"


def object_uri(bucket: str, key: str) -> str:
    """Path-style canonical URI. ``quote`` leaves ``/`` in the key and encodes everything else once."""
    key = validate_key(key)
    return "/" + quote(bucket, safe="") + "/" + quote(key, safe="/")


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date_stamp: str, region: str, service: str = "s3") -> bytes:
    key = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    key = hmac.new(key, region.encode("utf-8"), hashlib.sha256).digest()
    key = hmac.new(key, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(key, b"aws4_request", hashlib.sha256).digest()


def _canonical_headers(headers: dict[str, str]) -> tuple[str, str]:
    items: list[tuple[str, str]] = []
    for name, value in headers.items():
        lowered = name.lower().strip()
        collapsed = " ".join(value.strip().split())
        items.append((lowered, collapsed))
    items.sort()
    canonical = "".join(f"{name}:{value}\n" for name, value in items)
    signed = ";".join(name for name, _ in items)
    return canonical, signed


def build_authorization(
    method: str,
    canonical_uri: str,
    headers: dict[str, str],
    body_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    amz_date: str,
    service: str = "s3",
) -> str:
    """SigV4 Authorization header value for a header-signed request with an empty query string."""
    canonical_headers, signed_headers = _canonical_headers(headers)
    canonical_request = "\n".join((
        method.upper(),
        canonical_uri,
        "",
        canonical_headers,
        signed_headers,
        body_hash,
    ))
    date_stamp = amz_date[:8]
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join((
        _ALGORITHM,
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ))
    signature = hmac.new(
        signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        f"{_ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


class S3Storage:
    """Path-style client. ``region`` blank is signed as ``us-east-1`` (the SigV4 default)."""

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = (region or "us-east-1").strip() or "us-east-1"
        self._host = endpoint_host(self.endpoint)

    def __repr__(self) -> str:
        return f"S3Storage(endpoint={self.endpoint!r}, bucket={self.bucket!r}, region={self.region!r})"

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        resp = await self._request("PUT", key, data=data, content_type=content_type)
        if resp.status_code not in (200, 201, 204):
            raise StorageError(f"s3 PUT failed: HTTP {resp.status_code}")

    async def get(self, key: str) -> bytes | None:
        resp = await self._request("GET", key)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise StorageError(f"s3 GET failed: HTTP {resp.status_code}")
        return resp.content

    async def delete(self, key: str) -> None:
        resp = await self._request("DELETE", key)
        if resp.status_code not in (200, 202, 204, 404):
            raise StorageError(f"s3 DELETE failed: HTTP {resp.status_code}")

    async def exists(self, key: str) -> bool:
        resp = await self._request("HEAD", key)
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise StorageError(f"s3 HEAD failed: HTTP {resp.status_code}")
        return True

    async def _request(self, method: str, key: str, data: bytes | None = None, content_type: str | None = None) -> httpx.Response:
        endpoint = httpx.URL(self.endpoint)
        prefix = quote(endpoint.raw_path.decode("ascii").rstrip("/"), safe="/%")
        url = endpoint.copy_with(raw_path=(prefix + object_uri(self.bucket, key)).encode("ascii"))
        uri = url.raw_path.decode("ascii")
        body = data if data is not None else b""
        body_hash = payload_hash(body) if method == "PUT" else _EMPTY_PAYLOAD_HASH
        amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        headers = {
            "host": self._host,
            "x-amz-content-sha256": body_hash,
            "x-amz-date": amz_date,
        }
        if content_type:
            headers["content-type"] = content_type
        headers["authorization"] = build_authorization(
            method, uri, headers, body_hash, self.access_key, self.secret_key, self.region, amz_date,
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                return await client.request(
                    method,
                    url,
                    content=body if method == "PUT" else None,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            # The exception text can carry the request, including the Authorization header.
            logger.warning("s3 %s failed: %s", method, type(exc).__name__)
            raise StorageError(f"s3 {method} failed: {type(exc).__name__}") from None
