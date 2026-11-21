import asyncio
import uuid

from fastapi import Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rate_limit import PointBudget
from app.db.session import get_db
from app.core.errors import AppError
from app.models.cams import Cam
from app.services import webrtc_manager
from app.services.cluster import view_proxy
from app.services.cluster.identity import worker_enabled

from app.routers.cams._common import _get_cam_for_access, router



WEBRTC_BUDGET = PointBudget("cams_webrtc", capacity=20, per_minute=20)


async def _receive_or_end(websocket: WebSocket, ended: asyncio.Event):
    """Next client message, or None as soon as the video stream ends."""
    if ended.is_set():
        return None
    receive = asyncio.ensure_future(websocket.receive_json())
    waiter = asyncio.ensure_future(ended.wait())
    try:
        await asyncio.wait({receive, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if receive.done():
            return receive.result()
        return None
    finally:
        for task in (receive, waiter):
            if not task.done():
                task.cancel()
        await asyncio.gather(receive, waiter, return_exceptions=True)


@router.websocket("/{id}/webrtc")
async def webrtc_stream(
    websocket: WebSocket,
    id: int,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    print(f"[WebRTC] New connection request for camera {id}")

    if not settings.WEBRTC_ENABLED:
        print(f"[WebRTC] WebRTC is disabled")
        await websocket.close(code=1008, reason="WebRTC стриминг отключен")
        return

    # A trusted internal hop was already authorized by the receiving node.
    # Clustering off never sets this (trusted_proxied is false), so the auth
    # block below is exactly the previous behavior.
    if view_proxy.trusted_proxied(websocket):
        cam = await db.get(Cam, id)
        if cam is None:
            await websocket.close(code=1008, reason="Нет доступа к камере")
            return
    else:
        # Auth + camera access: same rules as stream/ptz (owner/admin/invited, or a
        # matching 'stream' share token) via the shared helpers.
        from app.deps.auth import extract_token_flexible, authorize_device_token

        try:
            raw = extract_token_flexible(None, token)
            ctx = await authorize_device_token(raw, "cams", "stream", id, db,
                                               websocket.headers.get("user-agent", ""))
            cam = await _get_cam_for_access(id, ctx, db)
        except AppError as e:
            print(f"[WebRTC] Access denied: {e}")
            await websocket.close(code=1008, reason="Нет доступа к камере")
            return
        except Exception as e:
            print(f"[WebRTC] Auth error: {e}")
            await websocket.close(code=1008, reason="Ошибка авторизации")
            return

        try:
            await WEBRTC_BUDGET.charge(websocket, 1)
        except AppError:
            await websocket.close(code=1008, reason="Слишком много подключений")
            return

        target = await view_proxy.view_target(db, id)
        if target is not None:
            await view_proxy.proxy_websocket(websocket, target)
            return

    # This node is the actual media/signaling owner (or clustering is off).
    # Ingress nodes proxy above before consulting their own local capability.
    if not worker_enabled(settings, "webrtc"):
        await websocket.close(code=1008, reason="WebRTC отключён на этой ноде")
        return

    print(f"[WebRTC] Camera found: {cam.name} ({cam.addr}:{cam.port})")

    await websocket.accept()
    print(f"[WebRTC] WebSocket accepted")

    session_id = str(uuid.uuid4())
    # The video stream may end by itself (ingest restart, decoder died/stalled): then the
    # client is told to reconnect instead of staring at a dead picture.
    ended = asyncio.Event()
    ended_track = []

    def on_stream_end(track) -> None:
        ended_track.append(track)
        ended.set()
    print(f"[WebRTC] Session ID: {session_id}")

    try:
        # Create WebRTC peer connection
        print(f"[WebRTC] Creating peer connection...")
        pc = await webrtc_manager.create_session(cam, session_id, on_end=on_stream_end)
        print(f"[WebRTC] Peer connection created")

        # Create offer from server side
        print(f"[WebRTC] Creating server-side offer...")
        offer_sdp = await webrtc_manager.create_offer(session_id)
        print(f"[WebRTC] Offer created, sending to client...")

        # Send offer to client
        await websocket.send_json({"type": "offer", "sdp": offer_sdp})
        print(f"[WebRTC] Sent offer to client")

        # Handle signaling messages
        while True:
            try:
                message = await _receive_or_end(websocket, ended)
                if message is None:  # the video stream ended
                    track = ended_track[0]
                    print(f"[WebRTC] Stream ended ({track.end_reason}), closing session {session_id}")
                    await websocket.send_json({"type": "error", "message": (
                        "Поток перезапущен, переподключитесь" if track.reconnect
                        else "Поток камеры недоступен, переподключитесь позже")})
                    # 1012: service restart (reconnect right away); 1011: server error
                    await websocket.close(code=1012 if track.reconnect else 1011, reason="Поток завершён")
                    break
                print(f"[WebRTC] Received message: type={message.get('type')}")
            except WebSocketDisconnect:
                print(f"[WebRTC] WebSocket disconnected")
                break

            msg_type = message.get("type")

            if msg_type == "answer":
                sdp = message.get("sdp")
                if not sdp:
                    print(f"[WebRTC] Answer without SDP")
                    await websocket.send_json({"type": "error", "message": "SDP отсутствует"})
                    continue

                print(f"[WebRTC] Processing answer (SDP length: {len(sdp)})")
                try:
                    await webrtc_manager.handle_answer(session_id, sdp)
                    print(f"[WebRTC] Answer processed, connection established")
                except webrtc_manager.WebRTCStreamError as exc:
                    print(f"[WebRTC] Error handling answer: {exc}")
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    break

            elif msg_type == "ice_candidate":
                candidate = message.get("candidate")
                if candidate:
                    print(f"[WebRTC] Received ICE candidate")
                    await webrtc_manager.add_ice_candidate(session_id, candidate)

            else:
                print(f"[WebRTC] Unknown message type: {msg_type}")
                await websocket.send_json({"type": "error", "message": f"Неизвестный тип сообщения: {msg_type}"})

    except Exception as exc:
        print(f"[WebRTC] Exception in handler: {exc}")
        import traceback
        traceback.print_exc()
    finally:
        print(f"[WebRTC] Closing session {session_id}")
        await webrtc_manager.close_session(session_id)
        try:
            await websocket.close()
            print(f"[WebRTC] WebSocket closed")
        except Exception as e:
            print(f"[WebRTC] Error closing WebSocket: {e}")
