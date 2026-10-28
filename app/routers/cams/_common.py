import time
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import (
    BAD_REQUEST,
    FILE_NOT_FOUND,
    FORBIDDEN_RESOURCE,
    LOCKED,
    OBJECT_NOT_FOUND,
    PTZ_NOT_CONFIGURED,
    RECORDING_NOT_ALLOWED,
    SERVER_ERROR,
)
from app.core.config import settings
from app.core.errors import AppError
from app.core.rate_limit import PointBudget, rate_limit
from app.core.security import TokenPayload
from app.db.session import get_db
from app.deps.auth import AccessContext, require_access, require_access_qs, resolve_record_access, resolve_stream_access
from app.models.cams import Cam
from app.models.invitation import Invitation
from app.models.sharing import Sharing
from app.models.user import User
from app.schemas.cams import (
    CamCreate,
    CamOutFull,
    CamOnvifPatch,
    CamOutRestricted,
    CamPatch,
    CamRecordingOut,
    CamRecordingPatch,
)
from app.schemas.ptz import PTZCommandIn, PTZPositionOut
from app.schemas.recording import RecordingOut
from app.services import onvif_client, ptz_lock, push, recordings, snapshot, stream_manager, hls_manager, webrtc_manager

router = APIRouter(prefix="/v1/cams", tags=["cams"])

# name -> column; public (API) field names, used for sort_by / search_column
CAM_LIST_COLUMNS = {
    "id": Cam.id,
    "name": Cam.name,
    "address": Cam.addr,
    "protocol": Cam.proto,
    "enabled": Cam.enabled,
    "added_at": Cam.added_at,
    "updated_at": Cam.updated_at,
}
BULK_MAX = 100

CONTROL_FIELDS = (
    "control_protocol",
    "control_address",
    "control_port",
    "control_username",
    "control_password",
)


def _invitation_exists_clause(payload: TokenPayload):
    return exists(
        select(Invitation.id)
        .join(User, func.lower(User.email) == func.lower(Invitation.email))
        .where(
            Invitation.device_type == "cams",
            Invitation.device_id == Cam.id,
            Invitation.status == "accepted",
            User.id == payload.uid,
        )
    )


def _visibility_where(payload: TokenPayload):
    if payload.role == "admin":
        return None
    return or_(
        Cam.ownuser == payload.uid,
        and_(Cam.enabled == 1, _invitation_exists_clause(payload)),
    )


def _can_manage(cam: Cam, payload: TokenPayload) -> bool:
    return payload.role == "admin" or cam.ownuser == payload.uid


def _serialize(cam: Cam, payload: TokenPayload) -> dict:
    if payload.role == "admin" or cam.ownuser == payload.uid:
        return CamOutFull(
            id=cam.id,
            owner=cam.ownuser,
            name=cam.name,
            enabled=cam.enabled,
            notify=cam.notify,
            protocol=cam.proto,
            address=cam.addr,
            port=cam.port,
            path=cam.path,
            username=cam.username,
            password=cam.password,
            control_protocol=cam.control_proto,
            control_address=cam.control_addr,
            control_port=cam.control_port,
            control_username=cam.control_username,
            control_password=cam.control_password,
            record=cam.record or 0,
            record_type=cam.record_type,
            record_metadata=cam.record_meta,
            onvif_enabled=cam.onvif_enabled,
            onvif_snapshot_enabled=cam.onvif_snapshot_enabled,
            onvif_ptz_enabled=cam.onvif_ptz_enabled,
            updated_at=cam.updated_at,
            added_at=cam.added_at,
        ).model_dump()
    return CamOutRestricted(
        id=cam.id,
        owner=cam.ownuser,
        name=cam.name,
        enabled=cam.enabled,
        protocol=cam.proto,
        address=cam.addr,
        port=cam.port,
        path=cam.path,
        control_protocol=cam.control_proto,
        onvif_enabled=cam.onvif_enabled,
        onvif_snapshot_enabled=cam.onvif_snapshot_enabled,
        onvif_ptz_enabled=cam.onvif_ptz_enabled,
        record=cam.record or 0,
        record_type=cam.record_type,
        updated_at=cam.updated_at,
        added_at=cam.added_at,
    ).model_dump()


async def _get_visible_cam(id: int, payload: TokenPayload, db: AsyncSession) -> Cam:
    stmt = select(Cam).where(Cam.id == id)
    condition = _visibility_where(payload)
    if condition is not None:
        stmt = stmt.where(condition)
    cam = (await db.execute(stmt)).scalar_one_or_none()
    if cam is None:
        raise AppError(OBJECT_NOT_FOUND, "Camera not found")
    return cam


async def _get_cam_for_access(id: int, ctx: AccessContext, db: AsyncSession) -> Cam:
    """Normal-token access is still subject to ownership/invitation visibility;
    a share token already proves scoped access to this exact cam, so it skips that check."""
    if ctx.payload is not None:
        return await _get_visible_cam(id, ctx.payload, db)
    cam = await db.get(Cam, id)
    if cam is None:
        raise AppError(OBJECT_NOT_FOUND, "Camera not found")
    return cam


_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

_LONG_CACHE_SECONDS = 24 * 24 * 60 * 60  # 24 дня
_LONG_CACHE_HEADERS = {"Cache-Control": f"public, max-age={_LONG_CACHE_SECONDS}"}


