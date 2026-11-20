"""Length-prefixed frames for cluster fMP4.

Wire format (big-endian):

    uint32 body_length | body
    body = uint8 type | uint16 channel | payload

`channel` is a stream id inside this TCP connection. It is not the camera id.
SUBSCRIBE carries the camera id as a uint32 in the payload. A body longer than
MAX_FRAME_BODY (8 MiB) is illegal: the reader raises and the connection closes
without reading the body.

HELLO payload: uint16 version | uint8 id_len | node_id | uint8 auth_len | auth.
auth is empty, or 32 bytes. The server's auth is a random nonce. The client's
auth is HMAC-SHA256(CLUSTER_SECRET, nonce). An empty secret skips HMAC and is
only valid for a loopback peer (see is_loopback_peer).

TLS is not applied to these frames. CLUSTER_SECRET authenticates the handshake
and does not encrypt. The socket itself is opened in media_transport.py, which
is the only place a future TLS wrapper belongs. Until that wrapper exists, do
not carry this protocol across an untrusted network.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import struct

PROTO_VERSION = 1
MAX_FRAME_BODY = 8 * 1024 * 1024  # type + channel + payload
CHANNEL_BUFFER_BYTES = 8 * 1024 * 1024
NONCE_LEN = 32

TYPE_HELLO = 1
TYPE_SUBSCRIBE = 2
TYPE_UNSUBSCRIBE = 3
TYPE_INIT = 4
TYPE_FRAGMENT = 5
TYPE_RESET = 6
TYPE_STATE = 7
TYPE_ERROR = 8
TYPE_PING = 9
TYPE_PONG = 10

ERR_NOT_OWNER = b"not_owner"
ERR_UNKNOWN_CAM = b"unknown_cam"
ERR_UNAUTHORIZED = b"unauthorized"

# Hub state "connecting" is sent as "starting" (the name on the wire).
WIRE_STATES = (b"online", b"offline", b"starting")


class ProtocolError(Exception):
    pass


class FrameTooLarge(ProtocolError):
    pass


def is_loopback_host(host: str | None) -> bool:
    """True for localhost, 127.0.0.0/8, ::1 and IPv4-mapped loopback."""
    return is_loopback_peer(host)


def is_loopback_peer(host: str | None) -> bool:
    if not host:
        return False
    text = host.strip().strip("[]")
    if text.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped is not None and mapped.is_loopback


def peer_host(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    if not peer:
        return ""
    return str(peer[0])


def encode_frame(msg_type: int, channel: int, payload: bytes = b"") -> bytes:
    if not 0 <= channel <= 0xFFFF:
        raise ProtocolError("channel out of range")
    body = bytes((msg_type & 0xFF,)) + struct.pack(">H", channel) + payload
    if len(body) > MAX_FRAME_BODY:
        raise FrameTooLarge(len(body))
    return struct.pack(">I", len(body)) + body


def encode_hello(node_id: str, auth: bytes) -> bytes:
    raw = (node_id or "").encode("utf-8")[:64]
    if len(auth) not in (0, NONCE_LEN):
        raise ProtocolError("HELLO auth must be empty or 32 bytes")
    return struct.pack(">HB", PROTO_VERSION, len(raw)) + raw + bytes((len(auth),)) + auth


def decode_hello(payload: bytes) -> tuple[int, str, bytes]:
    if len(payload) < 4:
        raise ProtocolError("short HELLO")
    version, nid_len = struct.unpack(">HB", payload[:3])
    rest = payload[3:]
    if len(rest) < nid_len + 1:
        raise ProtocolError("short HELLO")
    node_id = rest[:nid_len].decode("utf-8", errors="replace")
    auth_len = rest[nid_len]
    auth = rest[nid_len + 1:]
    if auth_len not in (0, NONCE_LEN) or len(auth) != auth_len:
        raise ProtocolError("bad HELLO auth")
    return version, node_id, auth


def sign_nonce(secret: str, nonce: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), nonce, hashlib.sha256).digest()


def auth_ok(secret: str, nonce: bytes, auth: bytes) -> bool:
    expected = sign_nonce(secret, nonce)
    return len(auth) == len(expected) and hmac.compare_digest(auth, expected)


def encode_subscribe(cam_id: int) -> bytes:
    if not 0 <= cam_id <= 0xFFFFFFFF:
        raise ProtocolError("cam_id out of range")
    return struct.pack(">I", cam_id)


def decode_subscribe(payload: bytes) -> int:
    if len(payload) != 4:
        raise ProtocolError("SUBSCRIBE must be a uint32 cam_id")
    return struct.unpack(">I", payload)[0]


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    """Next frame, or ConnectionError if the peer closed. FrameTooLarge if the
    declared body exceeds MAX_FRAME_BODY (the body is not read)."""
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError as exc:
        raise ConnectionError("connection closed") from exc
    (size,) = struct.unpack(">I", header)
    if size < 3 or size > MAX_FRAME_BODY:
        raise FrameTooLarge(size)
    try:
        body = await reader.readexactly(size)
    except asyncio.IncompleteReadError as exc:
        raise ConnectionError("connection closed") from exc
    channel = struct.unpack(">H", body[1:3])[0]
    return body[0], channel, body[3:]


def parse_address(address: str, default_port: int) -> tuple[str, int]:
    """`host`, `host:port` or `[ipv6]:port`."""
    text = (address or "").strip()
    if not text:
        raise ProtocolError("empty node address")
    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if not host or not sep:
            raise ProtocolError("bad node address")
        if rest.startswith(":"):
            return host, int(rest[1:])
        if rest:
            raise ProtocolError("bad node address")
        return host, default_port
    if text.count(":") == 1:
        host, port_s = text.rsplit(":", 1)
        if port_s.isdigit():
            return host, int(port_s)
    return text, default_port
