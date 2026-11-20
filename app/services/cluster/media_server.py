"""Accept fMP4 subscriptions from other nodes. The owner's ingest is not modified:
a remote viewer is an ordinary CamHub subscriber, so has_viewers / no_viewers follow
it the same way as a local one.

A slow peer never blocks hub.feed. Each camera on a connection has a bounded
outbound buffer (default 8 MiB). Past that the camera is RESET and restarted from
the current init plus the latest keyframe, not from a growing copy of the GOP.

Ownership: SUBSCRIBE is served only when this node holds a live ingest lease whose
generation is the one the node is actually running the camera under. Otherwise the
peer gets ERROR not_owner and no media.

The listening socket is plain TCP (media_transport). Do not expose it off loopback
on an untrusted network until TLS wraps that socket. An empty CLUSTER_SECRET
accepts loopback peers only.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import deque
from typing import Any, Callable

from sqlalchemy import select

from app.core.config import settings as app_settings
from app.db import session as db_session
from app.models.node import Assignment
from app.services.cluster.media_protocol import (
    CHANNEL_BUFFER_BYTES,
    ERR_NOT_OWNER,
    ERR_UNAUTHORIZED,
    ERR_UNKNOWN_CAM,
    NONCE_LEN,
    PROTO_VERSION,
    TYPE_ERROR,
    TYPE_FRAGMENT,
    TYPE_HELLO,
    TYPE_INIT,
    TYPE_PING,
    TYPE_PONG,
    TYPE_RESET,
    TYPE_STATE,
    TYPE_SUBSCRIBE,
    TYPE_UNSUBSCRIBE,
    MAX_FRAME_BODY,
    FrameTooLarge,
    ProtocolError,
    auth_ok,
    decode_hello,
    decode_subscribe,
    encode_frame,
    encode_hello,
    is_loopback_host,
    is_loopback_peer,
    peer_host,
    read_frame,
)
from app.services.cluster.media_transport import start_server
from app.services.fmp4 import fragment_starts_with_non_sync
from app.services.ingest.hub import IngestHub

logger = logging.getLogger(__name__)

GenerationOf = Callable[[int], int | None]


def _default_generation(cam_id: int) -> int | None:
    from app.services.cluster.runner import get_runner

    runner = get_runner()
    if runner is None:
        return None
    held = runner._held.get(("ingest", cam_id))
    if held is None:
        return None
    return int(held.lease.generation)


def _key_suffix(gop: list[bytes], budget: int) -> list[bytes]:
    """Tail of `gop` that starts on a keyframe and fits in `budget` bytes."""
    if budget <= 0 or not gop:
        return []
    start = 0
    for i, frag in enumerate(gop):
        if not fragment_starts_with_non_sync(frag):
            start = i
    suffix = list(gop[start:])
    while suffix and sum(len(part) for part in suffix) > budget:
        if len(suffix) == 1:
            return suffix if len(suffix[0]) <= budget else []
        suffix.pop(0)
        if fragment_starts_with_non_sync(suffix[0]):
            return []
    return suffix


class _Channel:
    def __init__(self, channel: int, cam_id: int):
        self.channel = channel
        self.cam_id = cam_id
        self.buf: deque[bytes] = deque()
        self.nbytes = 0
        self.active = True
        self.sub = None
        self.task: asyncio.Task | None = None
        self.sent_state: str | None = None

    def clear(self) -> None:
        self.buf.clear()
        self.nbytes = 0


class _Conn:
    def __init__(self, server: "MediaServer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.peer = peer_host(writer)
        self._channels: dict[int, _Channel] = {}
        self._rr: deque[_Channel] = deque()
        self._control: deque[bytes] = deque()
        self._wake = asyncio.Event()
        self._closed = False
        self._did_close = False
        self._writer_task: asyncio.Task | None = None

    @property
    def buffered_bytes(self) -> int:
        return sum(ch.nbytes for ch in self._channels.values())

    def _note_peak(self) -> None:
        self.server.peak_buffered = max(self.server.peak_buffered, self.buffered_bytes)

    async def run(self) -> None:
        await self._handshake()
        self._writer_task = asyncio.create_task(self._write_loop(), name="cluster-media-write")
        try:
            while not self._closed:
                msg_type, channel, payload = await read_frame(self.reader)
                if msg_type == TYPE_SUBSCRIBE:
                    await self._on_subscribe(channel, payload)
                elif msg_type == TYPE_UNSUBSCRIBE:
                    await self._on_unsubscribe(channel)
                elif msg_type == TYPE_PING:
                    self._control.append(encode_frame(TYPE_PONG, channel, payload[:64]))
                    self._wake.set()
                elif msg_type in (TYPE_PONG, TYPE_HELLO):
                    continue
                else:
                    # Clients do not send media. Ignore unknown types rather than
                    # tearing down every camera on the connection.
                    continue
        finally:
            await self.close()

    async def _handshake(self) -> None:
        secret = (self.server.secret or "").strip()
        nonce = os.urandom(NONCE_LEN) if secret else b""
        hello = encode_frame(TYPE_HELLO, 0, encode_hello(self.server.node_id, nonce))
        self.writer.write(hello)
        await self.writer.drain()
        msg_type, _channel, payload = await asyncio.wait_for(read_frame(self.reader), timeout=10)
        if msg_type != TYPE_HELLO:
            raise ProtocolError("expected HELLO")
        version, remote_id, auth = decode_hello(payload)
        if version != PROTO_VERSION:
            raise ProtocolError(f"protocol version {version}")
        if secret:
            if not auth_ok(secret, nonce, auth):
                with contextlib.suppress(Exception):
                    self.writer.write(encode_frame(TYPE_ERROR, 0, ERR_UNAUTHORIZED))
                    await self.writer.drain()
                raise ProtocolError("unauthorized")
        elif auth:
            raise ProtocolError("unexpected auth on an empty CLUSTER_SECRET")
        logger.info("cluster media: peer %s node %s connected", self.peer, remote_id)

    async def close(self) -> None:
        if self._did_close:
            return
        self._did_close = True
        self._closed = True
        self._wake.set()
        for ch in list(self._channels.values()):
            ch.active = False
            if ch.sub is not None:
                ch.sub.close()
            if ch.task is not None and not ch.task.done():
                ch.task.cancel()
        tasks = [ch.task for ch in self._channels.values() if ch.task is not None]
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()
            tasks.append(self._writer_task)
        if tasks:
            await asyncio.wait(tasks, timeout=1.0)
        self._channels.clear()
        self._rr.clear()
        with contextlib.suppress(Exception):
            self.writer.close()
            await self.writer.wait_closed()

    async def _write_loop(self) -> None:
        try:
            while not self._closed:
                frame = self._pop_frame()
                if frame is None:
                    self._wake.clear()
                    frame = self._pop_frame()
                    if frame is None:
                        await self._wake.wait()
                        continue
                await self.server.output_open.wait()
                if self._closed:
                    return
                self.writer.write(frame)
                await self.writer.drain()
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, RuntimeError):
            self._closed = True
            self._wake.set()

    def _pop_frame(self) -> bytes | None:
        if self._control:
            return self._control.popleft()
        n = len(self._rr)
        for _ in range(n):
            ch = self._rr.popleft()
            self._rr.append(ch)
            if ch.buf:
                frame = ch.buf.popleft()
                ch.nbytes -= len(frame)
                if ch.nbytes < 0:
                    ch.nbytes = 0
                return frame
        return None

    def _enqueue(self, ch: _Channel, frame: bytes) -> None:
        ch.buf.append(frame)
        ch.nbytes += len(frame)
        if ch not in self._rr:
            self._rr.append(ch)
        self._note_peak()
        self._wake.set()

    def _send_error(self, channel: int, code: bytes) -> None:
        self._control.append(encode_frame(TYPE_ERROR, channel, code))
        self._wake.set()

    def _enqueue_reset(self, ch: _Channel, reason: str) -> None:
        ch.clear()
        frame = encode_frame(TYPE_RESET, ch.channel, reason.encode("utf-8", errors="replace")[:200])
        self._enqueue(ch, frame)

    async def _on_subscribe(self, channel: int, payload: bytes) -> None:
        try:
            cam_id = decode_subscribe(payload)
        except ProtocolError:
            self._send_error(channel, ERR_UNKNOWN_CAM)
            return
        if channel == 0 or cam_id == 0:
            self._send_error(channel, ERR_UNKNOWN_CAM)
            return
        if not await self.server.owns(cam_id):
            logger.info("cluster media: SUBSCRIBE cam %s from %s rejected (not owner)", cam_id, self.peer)
            self._send_error(channel, ERR_NOT_OWNER)
            return
        existing = self._channels.get(channel)
        if existing is not None:
            await self._stop_channel(channel)
        ch = _Channel(channel, cam_id)
        self._channels[channel] = ch
        self._rr.append(ch)
        ch.task = asyncio.create_task(self._pump(ch), name=f"cluster-media-relay-{cam_id}")
        logger.info("cluster media: relaying cam %s channel %s to %s", cam_id, channel, self.peer)

    async def _on_unsubscribe(self, channel: int) -> None:
        if channel not in self._channels:
            return
        cam_id = self._channels[channel].cam_id
        await self._stop_channel(channel)
        logger.info("cluster media: cam %s channel %s unsubscribed by %s", cam_id, channel, self.peer)

    async def _stop_channel(self, channel: int) -> None:
        ch = self._channels.pop(channel, None)
        if ch is None:
            return
        ch.active = False
        ch.clear()
        if ch.sub is not None:
            ch.sub.close()
        if ch.task is not None and ch.task is not asyncio.current_task() and not ch.task.done():
            ch.task.cancel()
            await asyncio.wait({ch.task}, timeout=1.0)
        with contextlib.suppress(ValueError):
            self._rr.remove(ch)

    async def _pump(self, ch: _Channel) -> None:
        hub = self.server.hub.get(ch.cam_id)
        sub = hub.subscribe()
        ch.sub = sub
        self._send_state(ch)
        try:
            while ch.active and not self._closed:
                try:
                    chunk = await sub.__anext__()
                except StopAsyncIteration:
                    if not ch.active or self._closed:
                        return
                    reason = sub.close_reason or "reset"
                    if reason == "closed":
                        return
                    if not await self.server.owns(ch.cam_id):
                        self._enqueue_reset(ch, "not_owner")
                        self._send_error(ch.channel, ERR_NOT_OWNER)
                        ch.active = False
                        return
                    self._enqueue_reset(ch, reason)
                    sub = hub.subscribe()
                    ch.sub = sub
                    self._send_state(ch)
                    continue
                if not ch.active:
                    return
                if not self._offer(ch, chunk):
                    if self._closed:
                        return
                    sub.close()
                    sub = self._catch_up(ch)
                    ch.sub = sub
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cluster media: relay failed for cam %s", ch.cam_id)
        finally:
            if ch.sub is not None:
                ch.sub.close()
                ch.sub = None

    def _offer(self, ch: _Channel, chunk: bytes) -> bool:
        """Queue one init or fragment. False means the buffer was reset and the
        caller must drop this subscription and catch up from a keyframe."""
        msg_type = TYPE_INIT if len(chunk) >= 8 and chunk[4:8] == b"ftyp" else TYPE_FRAGMENT
        try:
            frame = encode_frame(msg_type, ch.channel, chunk)
        except FrameTooLarge:
            logger.error("cluster media: cam %s frame exceeds %s bytes, closing", ch.cam_id, MAX_FRAME_BODY)
            self._closed = True
            self._wake.set()
            return False
        if ch.nbytes and ch.nbytes + len(frame) > self.server.buffer_bytes:
            self._enqueue_reset(ch, "overflow")
            return False
        self._enqueue(ch, frame)
        self._send_state(ch)
        return True

    def _catch_up(self, ch: _Channel) -> Any:
        """No await: snapshot and subscribe cannot race a feed() on this loop."""
        hub = self.server.hub.get(ch.cam_id)
        init = hub.init_segment
        gop = list(hub.gop)
        sub = hub.subscribe(replay=False)
        budget = self.server.buffer_bytes - ch.nbytes
        if init and budget > len(init) + 16:
            frame = encode_frame(TYPE_INIT, ch.channel, init)
            if ch.nbytes + len(frame) <= self.server.buffer_bytes:
                self._enqueue(ch, frame)
                budget = self.server.buffer_bytes - ch.nbytes
        for frag in _key_suffix(gop, max(0, budget - 16)):
            try:
                frame = encode_frame(TYPE_FRAGMENT, ch.channel, frag)
            except FrameTooLarge:
                break
            if ch.nbytes and ch.nbytes + len(frame) > self.server.buffer_bytes:
                break
            self._enqueue(ch, frame)
        self._send_state(ch)
        return sub

    def _send_state(self, ch: _Channel) -> None:
        state = self.server.hub.get(ch.cam_id).state
        wire = "starting" if state == "connecting" else state
        if wire not in ("online", "offline", "starting") or wire == ch.sent_state:
            return
        ch.sent_state = wire
        self._control.append(encode_frame(TYPE_STATE, ch.channel, wire.encode()))
        self._wake.set()


class MediaServer:
    def __init__(
        self,
        *,
        settings: Any = None,
        sessionmaker: Any = None,
        hub: IngestHub | None = None,
        node_id: str | None = None,
        generation_of: GenerationOf | None = None,
        clock: Callable[[], float] = time.time,
        buffer_bytes: int = CHANNEL_BUFFER_BYTES,
        host: str | None = None,
        port: int | None = None,
        secret: str | None = None,
    ) -> None:
        self.settings = settings or app_settings
        self._sessionmaker = sessionmaker
        self.hub = hub if hub is not None else _lazy_hub()
        self.node_id = node_id or _lazy_node_id(self.settings)
        self._generation_of = generation_of
        self._clock = clock
        self.buffer_bytes = buffer_bytes
        self.host = self.settings.CLUSTER_MEDIA_HOST if host is None else host
        self.port = int(self.settings.CLUSTER_MEDIA_PORT if port is None else port)
        self.secret = self.settings.CLUSTER_SECRET if secret is None else secret
        self.output_open = asyncio.Event()
        self.output_open.set()
        self.peak_buffered = 0
        self._server: asyncio.AbstractServer | None = None
        self._conns: set[_Conn] = set()

    async def owns(self, cam_id: int) -> bool:
        """Live ingest lease, and the generation this process is running the camera under."""
        gen = self._generation_of(cam_id) if self._generation_of is not None else _default_generation(cam_id)
        if gen is None:
            return False
        now = int(self._clock())
        sm = self._sessionmaker or db_session.AsyncSessionLocal
        async with sm() as db:
            row = (await db.execute(select(Assignment).where(
                Assignment.function == "ingest", Assignment.resource_id == cam_id,
            ))).scalar_one_or_none()
            if row is None:
                return False
            return (
                row.owner_node_id == self.node_id
                and int(row.generation) == int(gen)
                and int(row.expires_at) > now
            )

    async def start(self) -> None:
        if self._server is not None:
            return
        secret = (self.secret or "").strip()
        if not secret and not is_loopback_host(self.host):
            raise RuntimeError(
                f"CLUSTER_SECRET is empty: refusing to listen on {self.host}:{self.port}. "
                "TLS is not enabled, so the media stream would be sent in the clear. "
                "Bind CLUSTER_MEDIA_HOST to 127.0.0.1 or ::1, or set CLUSTER_SECRET."
            )
        self._server = await start_server(self.host, self.port, self._on_connection)
        sockets = ", ".join(str(s.getsockname()) for s in (self._server.sockets or []))
        logger.info(
            "cluster media: listening on %s (plain TCP, no TLS; secret %s)",
            sockets, "set" if secret else "empty, loopback peers only",
        )

    async def stop(self) -> None:
        self.output_open.set()
        server, self._server = self._server, None
        for conn in list(self._conns):
            await conn.close()
        if server is not None:
            server.close()
            await server.wait_closed()

    async def disconnect_all(self) -> None:
        """Drop accepted sockets but keep listening (reconnect tests)."""
        for conn in list(self._conns):
            with contextlib.suppress(Exception):
                conn.writer.close()

    @property
    def connection_count(self) -> int:
        return len(self._conns)

    @property
    def channel_count(self) -> int:
        return sum(len(c._channels) for c in self._conns)

    def buffered_bytes(self) -> int:
        return sum(c.buffered_bytes for c in self._conns)

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        host = peer_host(writer)
        secret = (self.secret or "").strip()
        if not secret and not is_loopback_peer(host):
            logger.warning(
                "cluster media: rejected %s (empty CLUSTER_SECRET, not loopback; stream is not encrypted)",
                host or "unknown",
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        conn = _Conn(self, reader, writer)
        self._conns.add(conn)
        try:
            await conn.run()
        except asyncio.CancelledError:
            raise
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, ProtocolError, FrameTooLarge) as exc:
            logger.info("cluster media: %s closed (%s)", host or "peer", exc)
        except Exception:
            logger.exception("cluster media: connection from %s failed", host or "peer")
        finally:
            self._conns.discard(conn)
            await conn.close()


def _lazy_hub() -> IngestHub:
    from app.services.ingest import get_hub
    return get_hub()


def _lazy_node_id(settings: Any) -> str:
    from app.services.cluster.identity import get_node_id
    return get_node_id(settings)


_server: MediaServer | None = None


async def start() -> None:
    """Listen when CLUSTER_ENABLED. No-op otherwise (nothing binds)."""
    global _server
    if not app_settings.CLUSTER_ENABLED or _server is not None:
        return
    server = MediaServer()
    await server.start()
    _server = server


async def stop() -> None:
    global _server
    server, _server = _server, None
    if server is None:
        return
    try:
        await server.stop()
    except Exception:
        logger.exception("cluster media: failed to stop listener")


def get_server() -> MediaServer | None:
    return _server
