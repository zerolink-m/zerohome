"""Tuya IoT Cloud: pulsar (split from legacy tuya_client.py)."""

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


from app.services.tuya.constants import _LOGGER

# ---------------------------------------------------------------------------
# Pulsar consumer (optional)
# ---------------------------------------------------------------------------

try:
    import pulsar as _pulsar  # type: ignore
    _PULSAR_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dep
    _pulsar = None
    _PULSAR_AVAILABLE = False

try:
    from Crypto.Cipher import AES as _AES  # type: ignore
    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dep
    _AES = None
    _CRYPTO_AVAILABLE = False


_PULSAR_BROKERS: dict[str, str] = {
    "cn": "pulsar+ssl://mqe.tuyacn.com:7285/",
    "us": "pulsar+ssl://mqe.tuyaus.com:7285/",
    "eu": "pulsar+ssl://mqe.tuyaeu.com:7285/",
    "in": "pulsar+ssl://mqe.tuyain.com:7285/",
}


def _unpad_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 16 or data[-pad:] != bytes([pad]) * pad:
        # Best-effort: return unstripped payload rather than raising here.
        return data
    return data[:-pad]


class TuyaPulsarConsumer:
    """Async wrapper around Tuya's Pulsar message-queue subscription.

    Event formats observed on the wire:

    * DP report::

          {"dataId": "...", "devId": "...", "productKey": "...",
           "status": [{"code": "...", "value": ..., "t": <ms>}]}

    * Business event (device lifecycle)::

          {"bizCode": "online|offline|nameUpdate|bindUser|delete|
                        upgradeStatus|dpNameUpdate|deviceSubBind",
           "bizData": {...}, "devId": "...", "productKey": "...", "ts": <ms>}

    The consumer runs the synchronous ``pulsar`` client on a background thread
    and forwards decrypted, JSON-decoded messages to your async ``handler``
    through an ``asyncio.Queue``.  Each message is acked only after the
    handler returns without raising.

    Docs: https://developer.tuya.com/en/docs/iot/Pulsar-message-queue?id=Kanbxbcdo76fs
    """

    def __init__(
        self,
        access_id: str,
        access_secret: str,
        region: Literal["cn", "us", "eu", "in"],
        env: Literal["prod", "test"] = "prod",
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        if not _PULSAR_AVAILABLE:
            raise RuntimeError(
                "pulsar-client is not installed. "
                "Install it with `pip install pulsar-client` to use TuyaPulsarConsumer."
            )
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError(
                "pycryptodome is not installed. "
                "Install it with `pip install pycryptodome` to decrypt Tuya Pulsar payloads."
            )
        if region not in _PULSAR_BROKERS:
            raise ValueError(f"Unsupported region for Pulsar: {region!r}")

        self._access_id = access_id
        self._access_secret = access_secret
        self._broker = _PULSAR_BROKERS[region]
        self._env = env
        self._log = logger or _LOGGER.getChild("pulsar")

        suffix = "event" if env == "prod" else "event-test"
        self._topic = f"persistent://{access_id}/out/{suffix}"
        self._sub_name = f"tuya-sub-{access_id}"

        self._token = hashlib.md5(access_secret.encode()).hexdigest()[8:24]
        self._aes_key = access_secret[8:24].encode("utf-8")

        self._client: Any = None
        self._consumer: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[tuple[Any, dict[str, Any]] | None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._thread_stop = asyncio.Event()
        self._thread: Any = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Start the consumer.  Blocks until :meth:`stop` is called.

        The ``handler`` coroutine is invoked once per successfully decrypted
        event; if it raises, the underlying Pulsar message is *not* acked
        and Pulsar will redeliver it.
        """
        import threading

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=1000)

        auth = _pulsar.AuthenticationToken(self._token)
        self._client = _pulsar.Client(
            service_url=self._broker,
            authentication=auth,
            operation_timeout_seconds=30,
        )
        self._consumer = self._client.subscribe(
            topic=self._topic,
            subscription_name=self._sub_name,
            consumer_type=_pulsar.ConsumerType.Failover,
        )
        self._log.info(
            "Tuya Pulsar subscribed: broker=%s topic=%s sub=%s",
            self._broker, self._topic, self._sub_name,
        )

        self._thread = threading.Thread(
            target=self._blocking_reader, name="tuya-pulsar-reader", daemon=True
        )
        self._thread.start()

        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                msg, payload = item
                try:
                    await handler(payload)
                    self._consumer.acknowledge(msg)
                except Exception as e:  # noqa: BLE001
                    self._log.exception(
                        "Pulsar handler failed; negatively acknowledging: %r", e
                    )
                    with contextlib.suppress(Exception):
                        self._consumer.negative_acknowledge(msg)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Gracefully stop the consumer and release resources."""
        self._thread_stop.set()
        if self._queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)

        # Close pulsar resources (synchronous — fire and forget in a thread).
        def _close() -> None:
            with contextlib.suppress(Exception):
                if self._consumer is not None:
                    self._consumer.close()
            with contextlib.suppress(Exception):
                if self._client is not None:
                    self._client.close()

        await asyncio.to_thread(_close)
        self._log.info("Tuya Pulsar consumer stopped")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _blocking_reader(self) -> None:
        """Runs in a worker thread; forwards messages to the asyncio queue."""
        assert self._loop is not None and self._queue is not None
        loop = self._loop
        queue = self._queue
        consumer = self._consumer

        while not self._thread_stop.is_set():
            try:
                msg = consumer.receive(timeout_millis=1000)
            except Exception as e:  # noqa: BLE001 — pulsar raises non-standard errors
                # Timeouts are expected while idle; log everything else.
                text = str(e).lower()
                if "timeout" in text:
                    continue
                self._log.warning("Pulsar receive error: %r", e)
                continue

            try:
                payload = self._decrypt(msg.data())
            except Exception as e:  # noqa: BLE001
                self._log.exception("Failed to decrypt Pulsar message: %r", e)
                with contextlib.suppress(Exception):
                    consumer.negative_acknowledge(msg)
                continue

            fut = asyncio.run_coroutine_threadsafe(queue.put((msg, payload)), loop)
            try:
                fut.result(timeout=30)
            except Exception as e:  # noqa: BLE001
                self._log.exception(
                    "Failed to enqueue Pulsar message: %r", e
                )
                with contextlib.suppress(Exception):
                    consumer.negative_acknowledge(msg)

    def _decrypt(self, raw: bytes) -> dict[str, Any]:
        """Decrypt a Pulsar message body.

        The wire payload is a JSON envelope with a base64-encoded ``data`` field
        containing the AES-128-ECB ciphertext (PKCS#7 padded).  The key is the
        middle 16 characters of ``access_secret``.
        """
        envelope = json.loads(raw)
        data_b64 = envelope["data"]
        ciphertext = base64.b64decode(data_b64)

        cipher = _AES.new(self._aes_key, _AES.MODE_ECB)
        plain = _unpad_pkcs7(cipher.decrypt(ciphertext))
        return json.loads(plain)


# ---------------------------------------------------------------------------
# Demo / smoke section
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    # Example (not executed automatically):
    #
    # import asyncio, logging, os
    # logging.basicConfig(level=logging.DEBUG)
    #
    # async def main() -> None:
    #     async with TuyaCloudClient(
    #         access_id=os.environ["TUYA_ID"],
    #         access_secret=os.environ["TUYA_SECRET"],
    #         endpoint=TUYA_ENDPOINTS["eu"],
    #     ) as t:
    #         device = await t.get_device("bf1234567890abcdef")
    #         print("Device:", device.name, "online:", device.online)
    #
    #         status = await t.get_device_status(device.id)
    #         print("Status:", status)
    #
    #         await t.send_commands(
    #             device.id,
    #             [{"code": "switch_led", "value": True}],
    #             wait_confirm=True,
    #         )
    #
    # asyncio.run(main())
    #
    # # Pulsar example:
    # # consumer = TuyaPulsarConsumer(
    # #     access_id=os.environ["TUYA_ID"],
    # #     access_secret=os.environ["TUYA_SECRET"],
    # #     region="eu",
    # # )
    # # async def on_event(event: dict) -> None:
    # #     print("event:", event)
    # # asyncio.run(consumer.run(on_event))
    pass
