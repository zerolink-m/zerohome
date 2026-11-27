import asyncio
from contextlib import aclosing
import time

from fastapi import Depends, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import BAD_REQUEST, FORBIDDEN_RESOURCE, OBJECT_NOT_FOUND, PTZ_NOT_CONFIGURED, SERVER_ERROR
from app.core.errors import AppError
from app.core.introspection import supports
from app.core.rate_limit import PointBudget, rate_limit
from app.core.response import edited, single
from app.core.security import TokenPayload
from app.db.session import get_db
from app.deps.auth import AccessContext, require_access, resolve_stream_access
from app.deps.request_body import SingleBody
from app.models.cams import Cam
from app.schemas.cams import CamOnvifPatch
from app.schemas.ptz import PTZCommandIn, PTZPositionOut
from app.services import onvif_client, snapshot, stream_manager, ufanet_media

from app.routers.cams._common import _get_cam_for_access, router, single_body_for_route



STREAM_BUDGET = PointBudget("cams_stream", capacity=30, per_minute=30)
STREAM_OPEN_COST = 10
STREAM_ATTACH_COST = 3


class _LiveStreamResponse(StreamingResponse):
    async def stream_response(self, send) -> None:
        async def send_with_timeout(message) -> None:
            async with asyncio.timeout(30):
                await send(message)

        # Close the body iterator (and detach the viewer) even if a socket write stalls.
        async with aclosing(self.body_iterator):
            await super().stream_response(send_with_timeout)


@router.get("/{id}/stream")
@supports("streaming")
async def stream_cam(
    id: int,
    request: Request,
    ctx: AccessContext = Depends(resolve_stream_access),
    db: AsyncSession = Depends(get_db),
):
    cam = await _get_cam_for_access(id, ctx, db)

    if cam.connection_resource_id is not None:
        raise AppError(BAD_REQUEST, "Для облачной камеры доступен HLS")

    cost = STREAM_ATTACH_COST if await stream_manager.has_active_session(cam.id) else STREAM_OPEN_COST
    await STREAM_BUDGET.charge(request, cost)

    try:
        queue, session = await stream_manager.get_or_attach(cam)
    except stream_manager.StreamStartError as exc:
        raise AppError(SERVER_ERROR, str(exc))

    async def body():
        try:
            async for chunk in stream_manager.iter_queue(queue):
                yield chunk
        finally:
            await stream_manager.detach(cam.id, session, queue)

    # Existing streams intentionally keep the access granted when they were opened.
    return _LiveStreamResponse(body(), media_type="video/mp4")


@router.get("/{id}/ptz", dependencies=[Depends(rate_limit("cams_read", capacity=60, per_minute=60))])
async def get_ptz(
    id: int,
    ctx: AccessContext = Depends(resolve_stream_access),
    db: AsyncSession = Depends(get_db),
):
    cam = await _get_cam_for_access(id, ctx, db)
    if cam.control_proto is None:
        raise AppError(PTZ_NOT_CONFIGURED, "У камеры не задан control_proto — PTZ недоступен")

    position = await onvif_client.get_status(cam)
    return single("ptz", PTZPositionOut(**position).model_dump())


@router.post("/{id}/ptz", dependencies=[Depends(rate_limit("cams_ptz_move", capacity=15, per_minute=15))])
async def move_ptz(
    id: int,
    body: SingleBody[PTZCommandIn] = Depends(single_body_for_route("ptz", PTZCommandIn)),
    ctx: AccessContext = Depends(resolve_stream_access),
    db: AsyncSession = Depends(get_db),
):
    cam = await _get_cam_for_access(id, ctx, db)
    if cam.control_proto is None:
        raise AppError(PTZ_NOT_CONFIGURED, "У камеры не задан control_proto — PTZ недоступен")

    cmd = body.obj
    await onvif_client.absolute_move(cam, cmd.pan, cmd.tilt, cmd.zoom)

    return single("ptz", {"id": id, "pan": cmd.pan, "tilt": cmd.tilt, "zoom": cmd.zoom})


@router.get("/{id}/snapshot", dependencies=[Depends(rate_limit("cams_snapshot_live", capacity=600, per_minute=600))])
@supports("x-snapshot")
async def get_live_snapshot(
    id: int,
    ctx: AccessContext = Depends(resolve_stream_access),
    db: AsyncSession = Depends(get_db),
):
    """Get live snapshot from camera (ONVIF or RTSP fallback)."""
    cam = await _get_cam_for_access(id, ctx, db)

    try:
        if cam.connection_resource_id is not None:
            jpeg_data = await ufanet_media.screenshot(db, cam)
            return Response(content=jpeg_data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        jpeg_data = await snapshot.get_live_snapshot(cam)
        return Response(content=jpeg_data, media_type="image/jpeg")
    except snapshot.SnapshotError as exc:
        raise AppError(SERVER_ERROR, str(exc)) from exc


@router.patch("/{id}/onvif", dependencies=[Depends(rate_limit("cams_write", capacity=20, per_minute=20))])
async def update_onvif_settings(
    id: int,
    body: SingleBody[CamOnvifPatch] = Depends(single_body_for_route("onvif", CamOnvifPatch)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Update ONVIF settings for a camera. Body: data.onvif = {onvif_enabled?, ...}."""
    settings_update = body.obj

    cam = await db.get(Cam, id)
    if cam is None:
        raise AppError(OBJECT_NOT_FOUND, "Camera not found")

    if cam.connection_resource_id is not None:
        raise AppError(BAD_REQUEST, "Настройки ONVIF недоступны для облачной камеры")

    if payload.role != "admin" and payload.uid != cam.ownuser:
        raise AppError(FORBIDDEN_RESOURCE, "Нет доступа к этой камере")

    # Update only provided fields
    if settings_update.onvif_enabled is not None:
        cam.onvif_enabled = settings_update.onvif_enabled
    if settings_update.onvif_snapshot_enabled is not None:
        cam.onvif_snapshot_enabled = settings_update.onvif_snapshot_enabled
    if settings_update.onvif_ptz_enabled is not None:
        cam.onvif_ptz_enabled = settings_update.onvif_ptz_enabled

    cam.updated_at = int(time.time())
    await db.commit()

    # Invalidate ONVIF cache
    onvif_client.invalidate(cam.id)

    return edited("onvif", {
        "id": cam.id,
        "onvif_enabled": cam.onvif_enabled,
        "onvif_snapshot_enabled": cam.onvif_snapshot_enabled,
        "onvif_ptz_enabled": cam.onvif_ptz_enabled,
    })
