"""In-memory hub: per camera, the init segment + the last GOP + live fan-out.

Fed by the supervisor with raw ffmpeg stdout bytes. With no subscribers the data only
refreshes the buffer. A subscriber first receives init + the stored GOP, then live
fragments. Each subscriber has a byte-bounded queue; one that falls behind is cut off
(flag `overflowed`) instead of slowing the intake."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.config import settings
from app.services.fmp4 import Fmp4Splitter, fragment_starts_with_non_sync

logger = logging.getLogger(__name__)

STATE_CONNECTING = "connecting"
STATE_ONLINE = "online"
STATE_OFFLINE = "offline"

ViewersListener = Callable[[int, int], None]  # (cam_id, subscriber_count)


class Subscription:
    """Async iterator over one camera's fMP4 bytes: init, stored GOP, then live.

    Ends (StopAsyncIteration) when the stream restarts, the hub closes it or the
    subscriber overflowed (`overflowed` / `close_reason` tell why). Use as
    `async for chunk in sub`, and call `close()` (or use `async with`) when done."""

    def __init__(self, cam_id: int, hub: "CamHub", limit_bytes: int):
        self.cam_id = cam_id
        self._hub = hub
        self._limit = limit_bytes
        self._queue: deque[bytes] = deque()
        self._queued_bytes = 0
        self._wakeup = asyncio.Event()
        self.closed = False
        self.overflowed = False
        self.close_reason: str | None = None

    # -- producer side (hub) --
    def _push(self, data: bytes) -> None:
        if self.closed:
            return
        if self._queued_bytes + len(data) > self._limit:
            self.overflowed = True
            self._queue.clear()
            self._queued_bytes = 0
            self._finish("overflow")
            return
        self._queue.append(data)
        self._queued_bytes += len(data)
        self._wakeup.set()

    def _finish(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        self._wakeup.set()
        self._hub._detach(self)

    # -- consumer side --
    def close(self) -> None:
        self._finish("closed")

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> bytes:
        while True:
            if self._queue:
                data = self._queue.popleft()
                self._queued_bytes -= len(data)
                return data
            if self.closed:
                raise StopAsyncIteration
            self._wakeup.clear()
            await self._wakeup.wait()

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *exc) -> None:
        self.close()


class CamHub:
    def __init__(self, cam_id: int, hub: "IngestHub"):
        self.cam_id = cam_id
        self._owner = hub
        self.state: str = STATE_OFFLINE
        self.last_data_at: float | None = None  # time.monotonic()
        self.restarts: int = 0
        self.last_error: str | None = None
        self.bytes_in: int = 0
        self.init_segment: bytes | None = None
        self.gop: list[bytes] = []
        self.gop_bytes: int = 0
        self.subscribers: set[Subscription] = set()
        self._splitter = Fmp4Splitter()
        self.has_viewers = asyncio.Event()
        self.no_viewers = asyncio.Event()
        self.no_viewers.set()
        self.online = asyncio.Event()

    # -- intake (supervisor) --
    def feed(self, chunk: bytes) -> None:
        self.bytes_in += len(chunk)
        self.last_data_at = time.monotonic()
        fragments = self._splitter.feed(chunk)
        if self.init_segment is None and self._splitter.init_segment is not None:
            self.init_segment = self._splitter.init_segment
            for sub in list(self.subscribers):
                sub._push(self.init_segment)
        if self.init_segment is None:
            if self._splitter.init_done:
                self.last_error = "no init segment in ffmpeg output"
            return
        for fragment in fragments:
            self._on_fragment(fragment)
        if self.state != STATE_ONLINE:
            self.set_state(STATE_ONLINE)

    def feed_remote(self, init: bytes | None = None, fragment: bytes | None = None) -> None:
        """Whole init segment and/or one whole fragment from another node.

        Same fan-out as `feed` (GOP buffer, subscriber queues, online flag) but no
        byte scanner: the owner already cut the fMP4. Must not run together with
        `feed` — the node either owns ingest or it is pulling, never both.
        """
        if init:
            self.bytes_in += len(init)
            self.last_data_at = time.monotonic()
            if init != self.init_segment:
                self.init_segment = init
                self.gop = []
                self.gop_bytes = 0
                self._splitter = Fmp4Splitter()
                for sub in list(self.subscribers):
                    sub._push(init)
        if fragment:
            self.bytes_in += len(fragment)
            self.last_data_at = time.monotonic()
            if self.init_segment is not None:
                self._on_fragment(fragment)
        if self.init_segment is not None and self.state != STATE_ONLINE:
            self.set_state(STATE_ONLINE)

    def _on_fragment(self, fragment: bytes) -> None:
        if not fragment_starts_with_non_sync(fragment) or not self.gop:
            self.gop = []  # a keyframe fragment starts a new GOP
            self.gop_bytes = 0
        self.gop.append(fragment)
        self.gop_bytes += len(fragment)
        # Cap: drop from the front, but never below the newest fragment.
        while self.gop_bytes > self._owner.max_buffer_bytes and len(self.gop) > 1:
            self.gop_bytes -= len(self.gop.pop(0))
        for sub in list(self.subscribers):
            sub._push(fragment)

    def set_state(self, state: str) -> None:
        self.state = state
        if state == STATE_ONLINE:
            self.online.set()
        else:
            self.online.clear()

    def reset_stream(self, reason: str, state: str = STATE_OFFLINE) -> None:
        """The ffmpeg process ended: the next one has a new init segment and fresh
        timestamps, so everything buffered is stale and subscribers must reconnect."""
        self._splitter = Fmp4Splitter()
        self.init_segment = None
        self.gop = []
        self.gop_bytes = 0
        for sub in list(self.subscribers):
            sub._finish(reason)
        self.set_state(state)

    # -- subscribers --
    def subscribe(self, *, replay: bool = True) -> Subscription:
        sub = Subscription(self.cam_id, self, self._owner.subscriber_limit_bytes)
        if replay and self.init_segment is not None:
            sub._push(self.init_segment)
            for fragment in self.gop:
                sub._push(fragment)
        self.subscribers.add(sub)
        self.has_viewers.set()
        self.no_viewers.clear()
        self._owner._notify(self.cam_id, len(self.subscribers))
        return sub

    def _detach(self, sub: Subscription) -> None:
        if sub not in self.subscribers:
            return
        self.subscribers.discard(sub)
        if not self.subscribers:
            self.has_viewers.clear()
            self.no_viewers.set()
        self._owner._notify(self.cam_id, len(self.subscribers))

    @property
    def subscriber_count(self) -> int:
        return len(self.subscribers)

    def status(self) -> dict:
        age = None if self.last_data_at is None else round(time.monotonic() - self.last_data_at, 2)
        return {
            "state": self.state,
            "subscribers": self.subscriber_count,
            "last_data_age": age,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "bytes_in": self.bytes_in,
            "has_init": self.init_segment is not None,
            "gop_fragments": len(self.gop),
            "gop_bytes": self.gop_bytes,
        }


class IngestHub:
    """All cameras' hubs.

    Public API for consumers:
        sub = hub.subscribe(cam_id)      # async iterator of bytes (init + GOP + live)
        hub.subscriber_count(cam_id)
        hub.add_viewers_listener(cb)     # cb(cam_id, count) on every change
        hub.get(cam_id).has_viewers / .no_viewers / .online  (asyncio.Event)
        hub.status(cam_id) / hub.status_all()
    """

    def __init__(
        self,
        max_buffer_bytes: int | None = None,
        subscriber_limit_bytes: int | None = None,
    ):
        self.max_buffer_bytes = max_buffer_bytes or settings.INGEST_MAX_BUFFER_BYTES
        # a fresh subscriber is preloaded with up to a full GOP, so leave room for it
        self.subscriber_limit_bytes = subscriber_limit_bytes or self.max_buffer_bytes * 2
        self._cams: dict[int, CamHub] = {}
        self._listeners: list[ViewersListener] = []

    def get(self, cam_id: int) -> CamHub:
        hub = self._cams.get(cam_id)
        if hub is None:
            hub = self._cams[cam_id] = CamHub(cam_id, self)
        return hub

    def peek(self, cam_id: int) -> CamHub | None:
        return self._cams.get(cam_id)

    def subscribe(self, cam_id: int, *, replay: bool = True) -> Subscription:
        return self.get(cam_id).subscribe(replay=replay)

    def subscriber_count(self, cam_id: int) -> int:
        hub = self._cams.get(cam_id)
        return hub.subscriber_count if hub else 0

    def add_viewers_listener(self, listener: ViewersListener) -> None:
        self._listeners.append(listener)

    def remove_viewers_listener(self, listener: ViewersListener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def _notify(self, cam_id: int, count: int) -> None:
        for listener in list(self._listeners):
            try:
                listener(cam_id, count)
            except Exception:
                logger.exception("ingest: viewers listener failed")

    def drop(self, cam_id: int) -> None:
        hub = self._cams.pop(cam_id, None)
        if hub is not None:
            hub.reset_stream("removed")

    def status(self, cam_id: int) -> dict | None:
        hub = self._cams.get(cam_id)
        return hub.status() if hub else None

    def status_all(self) -> dict[int, dict]:
        return {cam_id: hub.status() for cam_id, hub in self._cams.items()}
