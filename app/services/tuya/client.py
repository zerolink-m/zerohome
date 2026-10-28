"""Tuya IoT Cloud: client (split from legacy tuya_client.py)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable,
    Literal,
    Mapping,
    Sequence,
)

import httpx
from pydantic import BaseModel, ConfigDict, Field


from app.services.tuya.constants import _CONFIRM_BACKOFF_MS, _DEFAULT_TIMEOUT, _LOGGER, _MAX_RETRIES, _RETRYABLE_CODES, _TOKEN_INVALID_CODES
from app.services.tuya.errors import TuyaError, _exception_for
from app.services.tuya.models import DPValue, Device, Function, FunctionValues, HomeInfo, LogEntry, Specifications, StreamURL, WebRTCConfig
from app.services.tuya.utils import _TokenState, _canonical_query, _hmac_sha256_upper, _mask, _serialize_body, _sha256_hex

# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class TuyaCloudClient:
    """Async client for the Tuya IoT Cloud OpenAPI.

    Example:
        >>> async with TuyaCloudClient(access_id, access_secret, "openapi.tuyaeu.com") as t:
        ...     dev = await t.get_device("bf1234567890abcdef")
        ...     print(dev.online, dev.name)
    """

    def __init__(
        self,
        access_id: str,
        access_secret: str,
        endpoint: str,
        *,
        logger: logging.Logger | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        lang: str = "en",
    ) -> None:
        if not access_id or not access_secret:
            raise ValueError("access_id and access_secret are required")
        if not endpoint:
            raise ValueError("endpoint is required")

        # Strip scheme if the caller accidentally passed a URL.
        endpoint = endpoint.replace("https://", "").replace("http://", "").strip("/")

        self._access_id = access_id
        self._access_secret = access_secret
        self._endpoint = endpoint
        self._base_url = f"https://{endpoint}"
        self._lang = lang
        self._log = logger or _LOGGER

        self._owns_http = http_client is None
        if http_client is not None:
            self._http = http_client
        else:
            # Enable HTTP/2 only if the optional `h2` package is available;
            # httpx raises ImportError otherwise.
            try:
                import h2  # type: ignore  # noqa: F401
                http2 = True
            except ImportError:
                http2 = False
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=timeout,
                http2=http2,
                limits=httpx.Limits(
                    max_keepalive_connections=20,
                    max_connections=100,
                    keepalive_expiry=30.0,
                ),
            )

        self._token = _TokenState()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "TuyaCloudClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client (only if we created it)."""
        if self._owns_http and not self._http.is_closed:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # Signature building
    # ------------------------------------------------------------------

    def _build_signature(
        self,
        *,
        method: str,
        path: str,
        query: Mapping[str, Any] | None,
        body_bytes: bytes,
        access_token: str | None,
        t_ms: str,
        nonce: str,
        signature_headers: Mapping[str, str] | None = None,
    ) -> str:
        """Build the ``sign`` header value using Tuya's "new signature" scheme."""
        body_hash = _sha256_hex(body_bytes)

        header_block = ""
        if signature_headers:
            header_block = "".join(f"{k}:{v}\n" for k, v in signature_headers.items())

        query_str = _canonical_query(query)
        url_line = path + (f"?{query_str}" if query_str else "")

        string_to_sign = (
            method.upper() + "\n"
            + body_hash + "\n"
            + header_block + "\n"
            + url_line
        )

        if access_token:
            payload = f"{self._access_id}{access_token}{t_ms}{nonce}{string_to_sign}"
        else:
            payload = f"{self._access_id}{t_ms}{nonce}{string_to_sign}"

        return _hmac_sha256_upper(self._access_secret, payload)

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _ensure_token(self) -> str:
        """Return a valid ``access_token``, refreshing/fetching under lock."""
        if self._token.is_valid():
            return self._token.access_token  # type: ignore[return-value]

        async with self._token.lock:
            if self._token.is_valid():
                return self._token.access_token  # type: ignore[return-value]

            if self._token.refresh_token:
                try:
                    await self._refresh_token_locked()
                except TuyaError as e:
                    self._log.warning(
                        "Refresh token failed (code=%s msg=%s); requesting a new one",
                        e.code, e.msg,
                    )
                    self._token.clear()
                    await self._get_token_locked()
            else:
                await self._get_token_locked()

            return self._token.access_token  # type: ignore[return-value]

    async def _get_token_locked(self) -> None:
        """Fetch a fresh token pair via ``GET /v1.0/token?grant_type=1``.
        Must be called with ``self._token.lock`` held.
        """
        result = await self._request(
            "GET",
            "/v1.0/token",
            query={"grant_type": 1},
            is_token_endpoint=True,
        )
        self._store_token(result)

    async def _refresh_token_locked(self) -> None:
        """Refresh the token pair; must be called with ``self._token.lock`` held."""
        refresh = self._token.refresh_token
        assert refresh, "refresh_token missing"
        # Old pair becomes invalid the moment the refresh call succeeds.
        result = await self._request(
            "GET",
            f"/v1.0/token/{refresh}",
            is_token_endpoint=True,
        )
        self._store_token(result)

    def _store_token(self, result: Mapping[str, Any]) -> None:
        self._token.access_token = result.get("access_token")
        self._token.refresh_token = result.get("refresh_token")
        self._token.uid = result.get("uid")
        expire_s = float(result.get("expire_time", 7200))
        self._token.expire_at = time.monotonic() + expire_s
        self._log.debug(
            "Stored Tuya token (uid=%s, expires_in=%ss, access_token=%s)",
            self._token.uid, expire_s, _mask(self._token.access_token),
        )

    # ------------------------------------------------------------------
    # Core request layer
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
        is_token_endpoint: bool = False,
    ) -> Any:
        """Perform a signed request and return the ``result`` field.

        Handles:
          * canonical query serialization for signature and URL
          * byte-exact body serialization
          * signature building
          * retry with exponential backoff for rate-limited codes
          * automatic single retry after token refresh for expired-token codes
          * conversion of Tuya error codes into typed exceptions.
        """
        body_bytes = _serialize_body(body)
        attempt = 0
        token_refreshed_once = False

        while True:
            attempt += 1

            access_token = None if is_token_endpoint else await self._ensure_token()

            t_ms = str(int(time.time() * 1000))
            nonce = uuid.uuid4().hex

            sign = self._build_signature(
                method=method,
                path=path,
                query=query,
                body_bytes=body_bytes,
                access_token=access_token,
                t_ms=t_ms,
                nonce=nonce,
            )

            headers: dict[str, str] = {
                "client_id": self._access_id,
                "sign": sign,
                "sign_method": "HMAC-SHA256",
                "t": t_ms,
                "nonce": nonce,
                "lang": self._lang,
                "Signature-Headers": "",
                "Content-Type": "application/json;charset=utf-8",
            }
            if access_token:
                headers["access_token"] = access_token

            log_query = {
                k: v for k, v in (query or {}).items() if k.lower() != "password"
            }
            started = time.perf_counter()
            try:
                response = await self._http.request(
                    method.upper(),
                    path,
                    params=_query_for_httpx(query),
                    content=body_bytes if body_bytes else None,
                    headers=headers,
                )
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt >= _MAX_RETRIES:
                    raise TuyaError(f"HTTP transport failure: {e!r}") from e
                delay = _backoff_delay(attempt)
                self._log.warning(
                    "Tuya %s %s transport error (attempt %d/%d): %r; sleeping %.2fs",
                    method, path, attempt, _MAX_RETRIES, e, delay,
                )
                await asyncio.sleep(delay)
                continue

            duration_ms = int((time.perf_counter() - started) * 1000)

            try:
                data = response.json()
            except ValueError as e:
                raise TuyaError(
                    f"Non-JSON response from Tuya: HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                ) from e

            tid = str(data.get("t") or "")
            self._log.debug(
                "Tuya %s %s q=%s -> success=%s code=%s tid=%s in %dms",
                method, path, log_query,
                data.get("success"), data.get("code"), tid, duration_ms,
            )

            if data.get("success"):
                return data.get("result")

            code = int(data.get("code") or 0)
            msg = str(data.get("msg") or "unknown error")

            self._log.error(
                "Tuya API error on %s %s: code=%s msg=%s tid=%s",
                method, path, code, msg, tid,
            )

            # Retry policy — expired/invalid token: refresh once, then retry.
            if code in _TOKEN_INVALID_CODES and not is_token_endpoint and not token_refreshed_once:
                token_refreshed_once = True
                async with self._token.lock:
                    self._token.clear()
                # Loop again to force _ensure_token.
                continue

            # Retry policy — rate limited / system busy.
            if code in _RETRYABLE_CODES and attempt < _MAX_RETRIES:
                delay = _backoff_delay(attempt)
                self._log.warning(
                    "Tuya retryable code %s on attempt %d/%d — sleeping %.2fs",
                    code, attempt, _MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
                continue

            raise _exception_for(code, msg, tid or None)

    # ------------------------------------------------------------------
    # Device management
    # ------------------------------------------------------------------

    async def get_device(self, device_id: str) -> Device:
        """Return device information.

        Docs: https://developer.tuya.com/en/docs/cloud/device-management?id=K9g6rfntdz78a#title-2-Query%20device%20information
        """
        result = await self._request("GET", f"/v1.0/devices/{device_id}")
        return Device.model_validate(result or {})

    async def list_user_devices(
        self,
        uid: str,
        last_row_key: str | None = None,
        page_size: int = 50,
    ) -> tuple[list[Device], str | None]:
        """Return the user's device list plus the next-cursor.

        Docs: https://developer.tuya.com/en/docs/cloud/device-management?id=K9g6rfntdz78a
        """
        query: dict[str, Any] = {"page_size": page_size}
        if last_row_key:
            query["last_row_key"] = last_row_key

        result = await self._request(
            "GET", f"/v1.0/users/{uid}/devices", query=query
        )
        # Tuya returns either a bare list or an object with ``devices`` + ``last_row_key``.
        if isinstance(result, list):
            return [Device.model_validate(d) for d in result], None
        devices = [Device.model_validate(d) for d in (result or {}).get("devices", [])]
        return devices, (result or {}).get("last_row_key")

    async def get_device_status(self, device_id: str) -> list[DPValue]:
        """Return the current DP status list.

        Docs: https://developer.tuya.com/en/docs/cloud/device-management?id=K9g6rfntdz78a#title-4-Query%20device%20status
        """
        result = await self._request("GET", f"/v1.0/devices/{device_id}/status")
        return [DPValue.model_validate(x) for x in (result or [])]

    async def get_device_specifications(self, device_id: str) -> Specifications:
        """Return functions and status specifications for a device.

        Docs: https://developer.tuya.com/en/docs/cloud/device-management?id=K9g6rfntdz78a
        """
        result = await self._request(
            "GET", f"/v1.0/devices/{device_id}/specifications"
        )
        return _parse_specifications(result or {})

    async def get_device_functions(self, device_id: str) -> list[Function]:
        """Return supported functions for a device.

        Docs: https://developer.tuya.com/en/docs/cloud/device-management?id=K9g6rfntdz78a
        """
        result = await self._request("GET", f"/v1.0/devices/{device_id}/functions")
        return _parse_functions((result or {}).get("functions", []))

    async def send_commands(
        self,
        device_id: str,
        commands: Sequence[Mapping[str, Any]],
        *,
        wait_confirm: bool = False,
        confirm_timeout_s: float = 5.0,
    ) -> bool:
        """Issue one or more DP commands atomically.

        Args:
            device_id: target device id.
            commands: list of ``{"code": <dp-code>, "value": <dp-value>}`` dicts.
            wait_confirm: when True, poll ``get_device_status`` until every
                          command's ``code`` reports back the expected value,
                          or until ``confirm_timeout_s`` elapses.
            confirm_timeout_s: total polling budget when ``wait_confirm`` is on.

        Returns:
            The server-side ``result`` field (True on success).

        Raises:
            TuyaError: when confirmation does not arrive within the budget.

        Docs: https://developer.tuya.com/en/docs/cloud/device-control?id=K95zu01ksols5
        """
        body = {"commands": list(commands)}
        result = await self._request(
            "POST", f"/v1.0/devices/{device_id}/commands", body=body
        )
        if not wait_confirm:
            return bool(result)

        expected = {c["code"]: c["value"] for c in commands}
        deadline = time.monotonic() + confirm_timeout_s
        for delay_ms in _CONFIRM_BACKOFF_MS:
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(delay_ms / 1000)
            statuses = await self.get_device_status(device_id)
            observed = {s.code: s.value for s in statuses}
            if all(observed.get(k) == v for k, v in expected.items()):
                return True

        raise TuyaError(
            f"send_commands: no confirmation for device={device_id} "
            f"within {confirm_timeout_s}s (expected={expected})"
        )

    async def rename_device(self, device_id: str, name: str) -> bool:
        """Rename a device.

        Docs: https://developer.tuya.com/en/docs/cloud/device-management?id=K9g6rfntdz78a
        """
        return bool(await self._request(
            "PUT", f"/v1.0/devices/{device_id}", body={"name": name}
        ))

    async def delete_device(self, device_id: str) -> bool:
        """Delete/unbind a device.

        Docs: https://developer.tuya.com/en/docs/cloud/device-management?id=K9g6rfntdz78a
        """
        return bool(await self._request("DELETE", f"/v1.0/devices/{device_id}"))

    async def reset_factory(self, device_id: str) -> bool:
        """Reset device to factory defaults.

        Docs: https://developer.tuya.com/en/docs/cloud/device-management?id=K9g6rfntdz78a
        """
        return bool(await self._request(
            "PUT", f"/v1.0/devices/{device_id}/reset-factory"
        ))

    # ------------------------------------------------------------------
    # Sub-devices (Zigbee / gateway)
    # ------------------------------------------------------------------

    async def list_sub_devices(self, gateway_id: str) -> list[Device]:
        """List sub-devices connected to a gateway.

        Docs: https://developer.tuya.com/en/docs/cloud/gateway-management?id=Kalmcohj35xh8
        """
        result = await self._request(
            "GET", f"/v1.0/devices/{gateway_id}/sub-devices"
        )
        return [Device.model_validate(d) for d in (result or [])]

    async def permit_join(self, gateway_id: str, seconds: int = 120) -> bool:
        """Open the gateway for a short pairing window.

        Docs: https://developer.tuya.com/en/docs/cloud/gateway-management?id=Kalmcohj35xh8
        """
        return await self.send_commands(
            gateway_id, [{"code": "permit_join", "value": int(seconds)}]
        )

    # ------------------------------------------------------------------
    # Device logs
    # ------------------------------------------------------------------

    async def get_device_logs(
        self,
        device_id: str,
        start_time_ms: int,
        end_time_ms: int,
        *,
        types: Iterable[int] | None = None,
        codes: Iterable[str] | None = None,
        size: int = 100,
        start_row_key: str | None = None,
    ) -> dict[str, Any]:
        """Read device event logs.

        Args:
            start_time_ms / end_time_ms: 13-digit ms timestamps (required).
            types: subset of 1..10 event categories.
            codes: DP codes to filter on.
            size: page size (max 100).
            start_row_key: pagination cursor from previous response.

        Returns:
            Raw ``result`` object with ``has_next``, ``next_row_key`` and
            ``logs: list[LogEntry]``.

        Docs: https://developer.tuya.com/en/docs/cloud/device-management?id=K9g6rfntdz78a
        """
        query: dict[str, Any] = {
            "start_time": start_time_ms,
            "end_time": end_time_ms,
            "size": min(size, 100),
        }
        if types:
            query["type"] = ",".join(str(t) for t in types)
        if codes:
            query["codes"] = ",".join(codes)
        if start_row_key:
            query["start_row_key"] = start_row_key

        result = await self._request(
            "GET", f"/v1.0/devices/{device_id}/logs", query=query
        )
        result = result or {}
        logs = [LogEntry.model_validate(x) for x in result.get("logs", [])]
        return {
            "has_next": bool(result.get("has_next")),
            "next_row_key": result.get("next_row_key"),
            "logs": logs,
        }

    # ------------------------------------------------------------------
    # User management
    # ------------------------------------------------------------------

    async def register_user(
        self,
        schema: str,
        country_code: str,
        username: str,
        password: str,
        username_type: int,
        *,
        nick_name: str | None = None,
        time_zone_id: str | None = None,
    ) -> str:
        """Register an app user.  ``password`` must already be MD5-hashed by the caller
        (see :func:`hash_password`).

        Docs: https://developer.tuya.com/en/docs/cloud/user-management?id=K9rmfjab3tp1p
        """
        body: dict[str, Any] = {
            "country_code": country_code,
            "username": username,
            "password": password,
            "username_type": username_type,
        }
        if nick_name is not None:
            body["nick_name"] = nick_name
        if time_zone_id is not None:
            body["time_zone_id"] = time_zone_id

        result = await self._request("POST", f"/v1.0/apps/{schema}/user", body=body)
        return str((result or {}).get("uid"))

    async def list_users(
        self,
        schema: str,
        page_no: int,
        page_size: int,
        *,
        username: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> dict[str, Any]:
        """Page through the app's user list.

        Docs: https://developer.tuya.com/en/docs/cloud/user-management?id=K9rmfjab3tp1p
        """
        query: dict[str, Any] = {
            "page_no": page_no,
            "page_size": min(page_size, 100),
        }
        if username:
            query["username"] = username
        if start_time is not None:
            query["start_time"] = start_time
        if end_time is not None:
            query["end_time"] = end_time
        return await self._request(
            "GET", f"/v2.0/apps/{schema}/users", query=query
        )

    async def get_user_info(self, uid: str) -> dict[str, Any]:
        """Return the user profile.

        Docs: https://developer.tuya.com/en/docs/cloud/user-management?id=K9rmfjab3tp1p
        """
        return await self._request("GET", f"/v1.0/users/{uid}/infos") or {}

    async def pre_delete_user(self, uid: str) -> bool:
        """Mark a user for deletion (soft-delete window before purge).

        Docs: https://developer.tuya.com/en/docs/cloud/user-management?id=K9rmfjab3tp1p
        """
        return bool(await self._request(
            "POST", f"/v1.0/users/{uid}/actions/pre-delete"
        ))

    # ------------------------------------------------------------------
    # Home
    # ------------------------------------------------------------------

    async def list_homes(self, uid: str) -> list[HomeInfo]:
        """List the user's homes.

        Docs: https://developer.tuya.com/en/docs/cloud/home-management?id=Ka6xf6oyu4pop
        """
        result = await self._request("GET", f"/v1.0/users/{uid}/homes")
        return [HomeInfo.model_validate(h) for h in (result or [])]

    async def get_home(self, home_id: int | str) -> dict[str, Any]:
        """Return home details (rooms, members).

        Docs: https://developer.tuya.com/en/docs/cloud/home-management?id=Ka6xf6oyu4pop
        """
        return await self._request("GET", f"/v1.0/homes/{home_id}") or {}

    async def list_home_devices(self, home_id: int | str) -> list[Device]:
        """List devices bound to a home.

        Docs: https://developer.tuya.com/en/docs/cloud/home-management?id=Ka6xf6oyu4pop
        """
        result = await self._request("GET", f"/v1.0/homes/{home_id}/devices")
        return [Device.model_validate(d) for d in (result or [])]

    # ------------------------------------------------------------------
    # Thing model (v2.0 shadow)
    # ------------------------------------------------------------------

    async def get_thing(self, device_id: str) -> dict[str, Any]:
        """Return the thing description (v2.0).

        Docs: https://developer.tuya.com/en/docs/cloud/device-control?id=K95zu01ksols5
        """
        return await self._request("GET", f"/v2.0/cloud/thing/{device_id}") or {}

    async def get_thing_properties(
        self, device_id: str, codes: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """Return the reported shadow properties.

        Docs: https://developer.tuya.com/en/docs/cloud/device-control?id=K95zu01ksols5
        """
        query = {"codes": ",".join(codes)} if codes else None
        return await self._request(
            "GET",
            f"/v2.0/cloud/thing/{device_id}/shadow/properties",
            query=query,
        ) or {}

    async def issue_thing_properties(
        self, device_id: str, properties: Mapping[str, Any]
    ) -> bool:
        """Issue property updates.  Note: Tuya requires ``properties`` to be a
        **JSON-encoded string** value inside the request body.

        Docs: https://developer.tuya.com/en/docs/cloud/device-control?id=K95zu01ksols5
        """
        body = {"properties": json.dumps(dict(properties), ensure_ascii=False)}
        return bool(await self._request(
            "POST",
            f"/v2.0/cloud/thing/{device_id}/shadow/properties/issue",
            body=body,
        ))

    async def set_desired_properties(
        self, device_id: str, properties: Mapping[str, Any]
    ) -> bool:
        """Set *desired* shadow properties (queued until the device reconnects).

        Docs: https://developer.tuya.com/en/docs/cloud/device-control?id=K95zu01ksols5
        """
        body = {"properties": json.dumps(dict(properties), ensure_ascii=False)}
        return bool(await self._request(
            "POST",
            f"/v2.0/cloud/thing/{device_id}/shadow/properties/desired",
            body=body,
        ))

    async def invoke_thing_action(
        self,
        device_id: str,
        code: str,
        input_params: Mapping[str, Any] | None = None,
    ) -> Any:
        """Invoke a thing-model action.

        Docs: https://developer.tuya.com/en/docs/cloud/device-control?id=K95zu01ksols5
        """
        body = {
            "actions": [
                {"code": code, "inputParams": dict(input_params or {})}
            ]
        }
        return await self._request(
            "POST", f"/v2.0/cloud/thing/{device_id}/shadow/actions", body=body
        )

    async def get_thing_model(self, device_id: str) -> dict[str, Any]:
        """Return the full thing-model description (services, properties, actions, events).

        Docs: https://developer.tuya.com/en/docs/cloud/device-control?id=K95zu01ksols5
        """
        return await self._request(
            "GET", f"/v2.0/cloud/thing/{device_id}/model"
        ) or {}

    # ------------------------------------------------------------------
    # Camera
    # ------------------------------------------------------------------

    async def get_webrtc_config(self, device_id: str) -> WebRTCConfig:
        """Return the WebRTC negotiation payload for a camera.

        Docs: https://developer.tuya.com/en/docs/cloud/f7cfe74d38?id=Kavgs62tj4gs4
        """
        result = await self._request(
            "GET", f"/v1.0/devices/{device_id}/webrtc-configs"
        )
        return WebRTCConfig.model_validate(result or {})

    async def allocate_stream(
        self, device_id: str, stream_type: Literal["rtsp", "hls"]
    ) -> StreamURL:
        """Allocate an RTSP or HLS stream URL for a camera.

        Docs: https://developer.tuya.com/en/docs/cloud/f7cfe74d38?id=Kavgs62tj4gs4
        """
        result = await self._request(
            "POST",
            f"/v1.0/devices/{device_id}/stream/actions/allocate",
            body={"type": stream_type},
        )
        return StreamURL.model_validate(result or {})


# ---------------------------------------------------------------------------
# Helpers used by the client
# ---------------------------------------------------------------------------


def _query_for_httpx(query: Mapping[str, Any] | None) -> list[tuple[str, str]] | None:
    """Return a sorted list of (k, v) pairs matching the canonical form used
    for the signature.  We MUST pass params in the same order to httpx so the
    URL that hits the wire matches the one we signed.
    """
    if not query:
        return None
    items: list[tuple[str, str]] = []
    for k, v in query.items():
        if v is None:
            continue
        if isinstance(v, bool):
            items.append((k, "true" if v else "false"))
        elif isinstance(v, (list, tuple)):
            for item in v:
                items.append((k, str(item)))
        else:
            items.append((k, str(v)))
    items.sort(key=lambda kv: kv[0])
    return items


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter: base 0.5s, capped at 8s."""
    base = min(0.5 * (2 ** (attempt - 1)), 8.0)
    return base + random.uniform(0, base / 2)


def _parse_functions(raw: Sequence[Mapping[str, Any]]) -> list[Function]:
    out: list[Function] = []
    for f in raw:
        values = f.get("values")
        parsed: dict[str, Any] | None = None
        if isinstance(values, str) and values:
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(values)
        out.append(
            Function(
                code=f.get("code"),
                name=f.get("name"),
                desc=f.get("desc"),
                type=f.get("type"),
                values=FunctionValues(
                    raw=values if isinstance(values, str) else None,
                    parsed=parsed,
                ),
            )
        )
    return out


def _parse_specifications(raw: Mapping[str, Any]) -> Specifications:
    return Specifications(
        category=raw.get("category"),
        functions=_parse_functions(raw.get("functions", [])),
        status=_parse_functions(raw.get("status", [])),
    )
