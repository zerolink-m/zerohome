"""Pull fMP4 from the node that owns a camera.

One TCP connection per destination node, reused for every camera that node owns.
The connection is redialed with a growing delay after a drop; each camera is
subscribed again and the caller is told (on_reset "reconnect") so the local hub
can drop the stale init.

CLUSTER_SECRET empty: dialling a non-loopback address raises MediaRefused before
any socket is opened. The bytes are not encrypted; TLS belongs in media_transport
and is not enabled.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from app.services.cluster.media_protocol import (
    PROTO_VERSION,
    TYPE_ERROR,
    TYPE_FRAGMENT,
    TYPE_HELLO,
    TYPE_INIT,
    TYPE_PONG,
    TYPE_RESET,
    TYPE_STATE,
    TYPE_SUBSCRIBE,
    TYPE_UNSUBSCRIBE,
    ProtocolError,
    decode_hello,
    encode_frame,
    encode_hello,
    encode_subscribe,
    is_loopback_host,
    parse_address,
    read_frame,
    sign_nonce,
)
from app.services.cluster.media_transport import open_connection

logger = logging.getLogger(__name__)

BytesCb = Callable[[int, bytes], None]
TextCb = Callable[[int, str], None]


class MediaRefused(RuntimeError):
    """Refusing to send camera media to a non-loopback address with an empty secret."""


class _Cam:
    def __init__(self, cam_id: int, channel: int):
        self.cam_id = cam_id
        self.channel = channel
        self.need_init = True
        self.started = False  # True after the first successful session subscribed it


class _Peer:
    def __init__(self, client: "MediaClient", node_id: str, host: str, port: int):
        self.client = client
        self.node_id = node_id
        self.host = host
        self.port = port
        self.cams: dict[int, _Cam] = {}
        self._next_channel = 1
        self._send_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ready = asyncio.Event()
        self.stopped = False
        self.connected = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self.stopped = False
            self._task = asyncio.create_task(self._run(), name=f"cluster-media-client-{self.node_id}")

    async def close(self) -> None:
        self.stopped = True
        self._ready.set()
        writer = self._writer
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=2.0)
        self.connected = False
        self._ready.clear()

    def _alloc_channel(self) -> int:
        used = {cam.channel for cam in self.cams.values()}
        channel = self._next_channel
        while channel == 0 or channel in used:
            channel = 1 if channel >= 0xFFFF else channel + 1
        self._next_channel = 1 if channel >= 0xFFFF else channel + 1
        return channel

    async def add(self, cam_id: int) -> None:
        cam = self.cams.get(cam_id)
        if cam is None:
            cam = _Cam(cam_id, self._alloc_channel())
            self.cams[cam_id] = cam
        if self.connected and self._writer is not None:
            cam.need_init = True
            await self._send(self._writer, TYPE_SUBSCRIBE, cam.channel, encode_subscribe(cam_id))

    async def remove(self, cam_id: int) -> None:
        cam = self.cams.pop(cam_id, None)
        if cam is None or not self.connected or self._writer is None:
            return
        with contextlib.suppress(ConnectionError, OSError, RuntimeError):
            await self._send(self._writer, TYPE_UNSUBSCRIBE, cam.channel, b"")

    async def _run(self) -> None:
        delay = self.client.reconnect_min
        while not self.stopped:
            try:
                await self._session()
                delay = self.client.reconnect_min
            except asyncio.CancelledError:
                raise
            except MediaRefused as exc:
                logger.error("cluster media: %s", exc)
                self.stopped = True
                break
            except Exception as exc:
                if self.stopped:
                    break
                logger.info("cluster media: connection to %s (%s:%s) dropped: %s",
                            self.node_id, self.host, self.port, exc)
            self.connected = False
            self._ready.clear()
            if self.stopped:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.client.reconnect_max)

    async def _session(self) -> None:
        # Checked again here so a reconnect cannot outrun a config that forbids it.
        self.client.check_target(self.host)
        reader, writer = await open_connection(self.host, self.port)
        self._writer = writer
        try:
            await self._handshake(reader, writer)
            self.connected = True
            self._ready.set()
            for cam in list(self.cams.values()):
                if cam.started:
                    cam.need_init = True
                    self.client.on_reset(cam.cam_id, "reconnect")
                cam.started = True
                await self._send(writer, TYPE_SUBSCRIBE, cam.channel, encode_subscribe(cam.cam_id))
            await self._read(reader, writer)
        finally:
            self.connected = False
            self._ready.clear()
            self._writer = None
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        msg_type, _channel, payload = await asyncio.wait_for(read_frame(reader), timeout=10)
        if msg_type != TYPE_HELLO:
            raise ProtocolError("expected server HELLO")
        version, _remote, nonce = decode_hello(payload)
        if version != PROTO_VERSION:
            raise ProtocolError(f"protocol version {version}")
        secret = (self.client.secret or "").strip()
        if secret:
            if len(nonce) != 32 or not nonce:
                raise ProtocolError("server HELLO has no nonce")
            auth = sign_nonce(secret, nonce)
        else:
            if nonce:
                raise ProtocolError("server offered a nonce but CLUSTER_SECRET is empty")
            auth = b""
        await self._send(writer, TYPE_HELLO, 0, encode_hello(self.client.node_id, auth))

    async def _read(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while not self.stopped:
            msg_type, channel, payload = await read_frame(reader)
            cam = next((c for c in self.cams.values() if c.channel == channel), None)
            if msg_type == TYPE_PONG or msg_type == TYPE_HELLO:
                continue
            if cam is None:
                continue
            if msg_type == TYPE_INIT:
                cam.need_init = False
                self.client.on_init(cam.cam_id, payload)
            elif msg_type == TYPE_FRAGMENT:
                if cam.need_init:
                    continue
                self.client.on_fragment(cam.cam_id, payload)
            elif msg_type == TYPE_RESET:
                reason = payload.decode("utf-8", errors="replace")[:200]
                cam.need_init = True
                self.client.on_reset(cam.cam_id, reason)
                # Same owner restarted the stream: ask for the new init. A lost
                # lease is not that; the caller picks a new owner.
                if reason != "not_owner":
                    await self._send(writer, TYPE_SUBSCRIBE, cam.channel, encode_subscribe(cam.cam_id))
            elif msg_type == TYPE_STATE:
                self.client.on_state(cam.cam_id, payload.decode("utf-8", errors="replace")[:32])
            elif msg_type == TYPE_ERROR:
                self.client.on_error(cam.cam_id, payload.decode("utf-8", errors="replace")[:64])

    async def _send(self, writer: asyncio.StreamWriter, msg_type: int, channel: int, payload: bytes) -> None:
        frame = encode_frame(msg_type, channel, payload)
        async with self._send_lock:
            writer.write(frame)
            await writer.drain()


class MediaClient:
    def __init__(
        self,
        *,
        node_id: str,
        secret: str = "",
        default_port: int = 4700,
        on_init: BytesCb,
        on_fragment: BytesCb,
        on_reset: TextCb,
        on_state: TextCb,
        on_error: TextCb,
        reconnect_min: float = 0.5,
        reconnect_max: float = 5.0,
    ) -> None:
        self.node_id = node_id
        self.secret = secret or ""
        self.default_port = default_port
        self.on_init = on_init
        self.on_fragment = on_fragment
        self.on_reset = on_reset
        self.on_state = on_state
        self.on_error = on_error
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self._peers: dict[str, _Peer] = {}
        self._by_cam: dict[int, str] = {}

    def check_target(self, host: str) -> None:
        if (self.secret or "").strip():
            return
        if is_loopback_host(host):
            return
        raise MediaRefused(
            f"CLUSTER_SECRET is empty: refusing to connect to {host}. "
            "TLS is not enabled, so camera media would travel in the clear. "
            "Set CLUSTER_SECRET before dialling a non-loopback address, or keep the "
            "other node on 127.0.0.1 / ::1."
        )

    async def subscribe(self, cam_id: int, node_id: str, address: str) -> None:
        host, port = parse_address(address, self.default_port)
        self.check_target(host)
        current = self._by_cam.get(cam_id)
        if current is not None and current != node_id:
            await self._peers[current].remove(cam_id)
            self._by_cam.pop(cam_id, None)
        peer = self._peers.get(node_id)
        if peer is not None and (peer.host, peer.port) != (host, port):
            await peer.close()
            peer = None
            self._peers.pop(node_id, None)
        if peer is None:
            peer = _Peer(self, node_id, host, port)
            self._peers[node_id] = peer
        self._by_cam[cam_id] = node_id
        # Register the camera before the dial runs, so the handshake cannot miss it.
        await peer.add(cam_id)
        peer.start()

    async def unsubscribe(self, cam_id: int) -> None:
        node_id = self._by_cam.pop(cam_id, None)
        if node_id is None:
            return
        peer = self._peers.get(node_id)
        if peer is not None:
            await peer.remove(cam_id)

    async def close(self) -> None:
        peers, self._peers = list(self._peers.values()), {}
        self._by_cam.clear()
        for peer in peers:
            await peer.close()

    @property
    def connection_count(self) -> int:
        return sum(1 for peer in self._peers.values() if not peer.stopped)

    def is_subscribed(self, cam_id: int) -> bool:
        return cam_id in self._by_cam
