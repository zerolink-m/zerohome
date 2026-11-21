"""Proxy HLS and WebRTC signaling to the node that holds the ingest lease.

The stage-3 relay is raw TCP fMP4. It does not carry playlists or WebRTC
signaling, so viewing is a separate HTTP/WebSocket proxy. With
CLUSTER_ENABLED=false every function here is a no-op and the routers keep
their previous behavior.

Owner: a live ``assignments`` row (function ``ingest``, resource_id = camera,
expires_at > now) plus ``nodes.address``. This node, a missing row, or an
empty address is served locally. ``parse_address`` (the same splitter the
media client uses) yields the host; the port in that address is the fMP4
port and is not used here. HTTP is ``CLUSTER_VIEW_HTTP_PORT`` or ``PORT``.

Loop avoidance and the second access check: the caller authenticates, then
sends ``X-Zerocams-Proxied: 1``. A trusted hop is served locally and is not
proxied again, and the access check is not repeated. The header is trusted
only when the TCP peer is loopback, or when ``X-Zerocams-Proxy-Sig`` is
HMAC-SHA256(CLUSTER_SECRET, ``METHOD\\nPATH\\nUNIX_TS``) and
``X-Zerocams-Proxy-Ts`` is within 30 seconds. With an empty secret only
loopback is trusted; an untrusted header is ignored. A non-loopback owner
is not dialed without a secret, because that hop could not be trusted and
the two nodes would proxy to each other.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocket, WebSocketState

from app.core.codes import SERVICE_UNAVAILABLE
from app.core.config import settings
from app.core.errors import AppError, error_response
from app.services.cluster.identity import get_node_id
from app.services.cluster.media_protocol import ProtocolError, is_loopback_host, parse_address

logger = logging.getLogger(__name__)

PROXIED_HEADER = "x-zerocams-proxied"
PROXY_TS_HEADER = "x-zerocams-proxy-ts"
PROXY_SIG_HEADER = "x-zerocams-proxy-sig"
PROXY_WINDOW_SECONDS = 30

_HOP_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "keep-alive",
    "te", "trailer", "upgrade", "proxy-connection", "proxy-authenticate",
    "proxy-authorization", PROXIED_HEADER, PROXY_TS_HEADER, PROXY_SIG_HEADER,
})
_PASS_RESPONSE = ("cache-control", "pragma", "expires")

# Tests inject these. Production leaves them None (real network).
http_transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None
ws_connector: Callable[..., object] | None = None

_OWNER_DOWN = "Узел с камерой недоступен"


class OwnerUnreachable(Exception):
    pass


@dataclass(frozen=True)
class ViewTarget:
    node_id: str
    host: str
    address: str


def _secret() -> str:
    return (settings.CLUSTER_SECRET or "").strip()


def _http_port() -> int:
    custom = int(settings.CLUSTER_VIEW_HTTP_PORT or 0)
    return custom if custom else int(settings.PORT)


def _netloc(host: str) -> str:
    bracket = f"[{host}]" if ":" in host else host
    return f"{bracket}:{_http_port()}"


def _peer_host(client) -> str:
    if not client:
        return ""
    host = getattr(client, "host", None)
    if host:
        return str(host)
    try:
        return str(client[0])
    except (TypeError, IndexError):
        return ""


def _method_of(conn) -> str:
    return (getattr(conn, "method", None) or "GET").upper()


def _path_of(conn) -> str:
    return conn.url.path


def sign_proxy(secret: str, method: str, path: str, ts: int) -> str:
    msg = f"{method.upper()}\n{path}\n{int(ts)}".encode()
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _signature_ok(conn) -> bool:
    secret = _secret()
    if not secret:
        return False
    ts_raw = conn.headers.get(PROXY_TS_HEADER, "")
    sig = conn.headers.get(PROXY_SIG_HEADER, "")
    if not ts_raw.isdigit() or not sig:
        return False
    ts = int(ts_raw)
    if abs(int(time.time()) - ts) > PROXY_WINDOW_SECONDS:
        return False
    expected = sign_proxy(secret, _method_of(conn), _path_of(conn), ts)
    if len(sig) != len(expected):
        return False
    return hmac.compare_digest(expected, sig)


def trusted_proxied(conn) -> bool:
    """True only when clustering is on and the internal hop header is trusted.

    Untrusted (or absent) means the caller is a normal client: check access
    and proxy if needed. Clustering off ignores the header entirely.
    """
    if not settings.CLUSTER_ENABLED:
        return False
    if (conn.headers.get(PROXIED_HEADER) or "").strip() != "1":
        return False
    if is_loopback_host(_peer_host(getattr(conn, "client", None))):
        return True
    return _signature_ok(conn)


def proxy_headers(method: str, path: str) -> dict[str, str]:
    headers = {PROXIED_HEADER: "1"}
    secret = _secret()
    if secret:
        ts = int(time.time())
        headers[PROXY_TS_HEADER] = str(ts)
        headers[PROXY_SIG_HEADER] = sign_proxy(secret, method, path, ts)
    return headers


def _unavailable() -> AppError:
    return AppError(SERVICE_UNAVAILABLE, _OWNER_DOWN, hint="Повторите запрос")


def _require_signed_hop(host: str) -> None:
    if _secret() or is_loopback_host(host):
        return
    logger.warning(
        "cluster view: not dialing %s without CLUSTER_SECRET; the owner would not trust the hop",
        host,
    )
    raise _unavailable()


async def view_target(db, cam_id: int) -> ViewTarget | None:
    """Remote ingest owner to proxy to, or None to serve locally.

    None when clustering is off, this node owns the live lease, there is no
    live lease, or the owner's address is empty.
    """
    if not settings.CLUSTER_ENABLED:
        return None
    from sqlalchemy import select

    from app.models.node import Assignment, Node

    now = int(time.time())
    row = (await db.execute(select(Assignment).where(
        Assignment.function == "ingest",
        Assignment.resource_id == cam_id,
        Assignment.expires_at > now,
    ))).scalar_one_or_none()
    if row is None or not row.owner_node_id:
        return None
    if row.owner_node_id == get_node_id(settings):
        return None
    node = await db.get(Node, row.owner_node_id)
    address = (node.address or "").strip() if node is not None else ""
    if not address:
        return None
    try:
        host, _media_port = parse_address(address, int(settings.CLUSTER_MEDIA_PORT))
    except (ProtocolError, ValueError):
        logger.warning("cluster view: owner %s has an unusable address", row.owner_node_id)
        raise _unavailable()
    return ViewTarget(node_id=row.owner_node_id, host=host, address=address)


def _forward_request_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() in _HOP_HEADERS:
            continue
        out[key] = value
    return out


def _response_headers(resp: httpx.Response) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in _PASS_RESPONSE:
        value = resp.headers.get(key)
        if value:
            out[key] = value
    return out


async def proxy_http(request: Request, target: ViewTarget) -> StreamingResponse:
    """Stream the owner's response. Status and Content-Type are passed through.

    The body is not rewritten: playlist segment names stay relative, so the
    player keeps requesting this node. Connection failure or a silence longer
    than the configured timeout becomes 503.
    """
    _require_signed_hop(target.host)
    path = request.url.path
    query = request.url.query
    url = f"http://{_netloc(target.host)}{path}"
    if query:
        url = f"{url}?{query}"
    headers = _forward_request_headers(request)
    headers.update(proxy_headers(request.method, path))
    connect = float(settings.CLUSTER_VIEW_CONNECT_TIMEOUT_SECONDS)
    read = float(settings.CLUSTER_VIEW_READ_TIMEOUT_SECONDS)
    timeout = httpx.Timeout(connect=connect, read=read, write=read, pool=connect)
    transport = http_transport_factory() if http_transport_factory is not None else None
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport)
    try:
        upstream = client.build_request(request.method, url, headers=headers)
        try:
            resp = await asyncio.wait_for(client.send(upstream, stream=True), timeout=connect + read)
        except (TimeoutError, httpx.TransportError, OSError) as exc:
            await _aclose(client)
            logger.warning(
                "cluster view: owner %s at %s did not answer (%s)",
                target.node_id, target.host, type(exc).__name__,
            )
            raise _unavailable() from exc
    except AppError:
        raise
    except Exception:
        await _aclose(client)
        raise

    # StopAsyncIteration must not escape a coroutine (PEP 479 turns it into RuntimeError).
    end = object()

    async def _next_chunk():
        try:
            return await agen.__anext__()
        except StopAsyncIteration:
            return end

    # No chunk_size: httpx would otherwise hold bytes until 64 KiB or EOF, which
    # buffers a whole segment before the client sees any of it.
    agen = resp.aiter_bytes().__aiter__()

    async def body() -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await asyncio.wait_for(_next_chunk(), timeout=read)
                if chunk is end:
                    break
                yield chunk
        except (TimeoutError, httpx.TransportError, OSError) as exc:
            logger.warning(
                "cluster view: owner %s at %s stalled (%s)",
                target.node_id, target.host, type(exc).__name__,
            )
        finally:
            await _aclose(resp)
            await _aclose(client)

    headers = _response_headers(resp)
    headers["content-type"] = resp.headers.get("content-type") or "application/octet-stream"
    return StreamingResponse(body(), status_code=resp.status_code, headers=headers)


async def _aclose(obj) -> None:
    close = getattr(obj, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        logger.debug("cluster view: close failed", exc_info=True)


async def proxy_websocket(websocket: WebSocket, target: ViewTarget) -> None:
    """Relay signaling both ways until either side closes. Media stays on the owner."""
    try:
        _require_signed_hop(target.host)
    except AppError:
        await _ws_unavailable(websocket)
        return
    path = websocket.url.path
    query = websocket.url.query
    url = f"ws://{_netloc(target.host)}{path}"
    if query:
        url = f"{url}?{query}"
    headers = {}
    # The browser's token is in the query. Forward Authorization if present;
    # drop anything the client tried to pass as our hop header.
    authorization = websocket.headers.get("authorization")
    if authorization:
        headers["authorization"] = authorization
    headers.update(proxy_headers("GET", path))
    try:
        upstream = await _connect_ws(url, headers)
    except OwnerUnreachable as exc:
        logger.warning(
            "cluster view: webrtc owner %s at %s unreachable (%s)",
            target.node_id, target.host, type(exc.__cause__ or exc).__name__,
        )
        await _ws_unavailable(websocket)
        return
    accepted = False
    try:
        await websocket.accept()
        accepted = True
        await _relay(websocket, upstream)
    finally:
        close = getattr(upstream, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                logger.debug("cluster view: upstream ws close failed", exc_info=True)
        if accepted and websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except Exception:
                logger.debug("cluster view: client ws close failed", exc_info=True)


async def _connect_ws(url: str, headers: dict[str, str]):
    if ws_connector is not None:
        return await ws_connector(url, headers)
    return await _library_ws(url, headers)


async def _library_ws(url: str, headers: dict[str, str]):
    from websockets.asyncio.client import connect as ws_connect

    connect_timeout = float(settings.CLUSTER_VIEW_CONNECT_TIMEOUT_SECONDS)
    try:
        conn = await asyncio.wait_for(
            ws_connect(
                url,
                additional_headers=list(headers.items()),
                proxy=None,
                open_timeout=connect_timeout,
                compression=None,
                user_agent_header="ZeroCams-View-Proxy",
            ),
            timeout=connect_timeout + 1.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise OwnerUnreachable(url) from exc

    class _Conn:
        async def send(self, data):
            await conn.send(data)

        def __aiter__(self):
            return self

        async def __anext__(self):
            from websockets.exceptions import ConnectionClosed

            try:
                return await conn.recv()
            except ConnectionClosed as exc:
                raise StopAsyncIteration from exc

        async def close(self):
            await conn.close()

    return _Conn()


async def _relay(websocket: WebSocket, upstream) -> None:
    async def client_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            data = message.get("bytes")
            if text is not None:
                await upstream.send(text)
            elif data is not None:
                await upstream.send(data)

    async def upstream_to_client() -> None:
        async for payload in upstream:
            if isinstance(payload, str):
                await websocket.send_text(payload)
            else:
                await websocket.send_bytes(payload)

    tasks = {
        asyncio.create_task(client_to_upstream(), name="view-proxy-c2u"),
        asyncio.create_task(upstream_to_client(), name="view-proxy-u2c"),
    }
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            # A relay error (peer gone) ends the session; it is not a 500.
            task.exception() if not task.cancelled() else None
    except Exception:
        for task in tasks:
            task.cancel()
        raise


async def _ws_unavailable(websocket: WebSocket) -> None:
    response = error_response(_unavailable())
    if "websocket.http.response" in websocket.scope.get("extensions", {}):
        try:
            await websocket.send_denial_response(response)
            return
        except RuntimeError:
            pass
    if websocket.client_state != WebSocketState.DISCONNECTED:
        try:
            await websocket.close(code=1013, reason=_OWNER_DOWN)
        except Exception:
            logger.debug("cluster view: ws denial close failed", exc_info=True)
