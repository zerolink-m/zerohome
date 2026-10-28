"""Motion detection API endpoints."""
import time

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import FORBIDDEN_RESOURCE, OBJECT_NOT_FOUND
from app.core.config import settings
from app.core.errors import AppError
from app.core.response import deleted, listing, single
from app.core.rate_limit import rate_limit
from app.core.security import TokenPayload
from app.db.session import get_db
from app.deps.auth import require_access
from app.deps.list_query import ListQuery, list_query
from app.models.cams import Cam
from app.models.motion_event import MotionEvent
from app.schemas.motion import MotionEventResponse
from app.services import motion_detection

router = APIRouter(prefix="/v1", tags=["motion"])

MOTION_FIELDS = ["id", "detected_at", "object_type", "confidence", "object_size", "license_plate"]
motion_events_query = list_query(
    model=MotionEvent, sortable=MOTION_FIELDS, searchable=MOTION_FIELDS, default_limit=50, max_limit=200,
)


def _event_out(e: MotionEvent) -> dict:
    return MotionEventResponse.model_validate(e).model_dump()


async def _check_cam_access(db: AsyncSession, cam_id: int, user_id: int, is_admin: bool):
    """Check if user has access to camera."""
    cam = await db.get(Cam, cam_id)
    if cam is None:
        raise AppError(OBJECT_NOT_FOUND, "Камера не найдена")

    if not is_admin and cam.ownuser != user_id:
        # Check if user has invitation
        from app.models.invitation import Invitation
        stmt = select(Invitation).where(
            Invitation.device_type == "cams",
            Invitation.device_id == cam_id,
            Invitation.status == "accepted",
        )
        result = await db.execute(stmt)
        invitations = result.scalars().all()

        # Get user email
        from app.models.user import User
        user = await db.get(User, user_id)
        if user is None or not any(inv.email.lower() == user.email.lower() for inv in invitations):
            raise AppError(FORBIDDEN_RESOURCE, "Нет доступа к этой камере")

    return cam


@router.get(
    "/cams/{cam_id}/motion/events",
    dependencies=[Depends(rate_limit("motion_read", capacity=60, per_minute=60))],
)
async def get_motion_events(
    cam_id: int,
    q: ListQuery = Depends(motion_events_query),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Get motion detection events for a camera."""
    await _check_cam_access(db, cam_id, payload.uid, payload.role == "admin")

    stmt = select(MotionEvent).where(MotionEvent.cam_id == cam_id)
    if not q.sort_by:  # default: newest first
        q.sort_by, q.order = ["detected_at"], ["descending"]
    events, total = await q.paginate(db, stmt)

    return listing("motion_events", [_event_out(e) for e in events], query=q, total=total)


@router.get(
    "/motion/events/{event_id}",
    dependencies=[Depends(rate_limit("motion_read", capacity=60, per_minute=60))],
)
async def get_motion_event(
    event_id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Get a specific motion event."""
    event = await db.get(MotionEvent, event_id)
    if event is None:
        raise AppError(OBJECT_NOT_FOUND, "Событие не найдено")

    # Check access to camera
    await _check_cam_access(db, event.cam_id, payload.uid, payload.role == "admin")

    return single("motion_event", _event_out(event))


@router.get(
    "/motion/events/{event_id}/photo",
    dependencies=[Depends(rate_limit("motion_photo", capacity=100, per_minute=60))],
)
async def get_motion_event_photo(
    event_id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Download photo of a motion event."""
    import os
    from fastapi.responses import FileResponse

    event = await db.get(MotionEvent, event_id)
    if event is None:
        raise AppError(OBJECT_NOT_FOUND, "Событие не найдено")

    # Check access to camera
    await _check_cam_access(db, event.cam_id, payload.uid, payload.role == "admin")

    # Build photo path
    photo_path = os.path.join(
        settings.motion_files_path,
        str(event.cam_id),
        event.photo_filename
    )

    if not os.path.exists(photo_path):
        raise AppError(OBJECT_NOT_FOUND, "Фото не найдено")

    return FileResponse(
        photo_path,
        media_type="image/jpeg",
        filename=event.photo_filename,
    )


@router.delete(
    "/motion/events/{event_id}",
    dependencies=[Depends(rate_limit("motion_write", capacity=30, per_minute=30))],
)
async def delete_motion_event(
    event_id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Delete a motion event (owner only)."""
    import os

    event = await db.get(MotionEvent, event_id)
    if event is None:
        raise AppError(OBJECT_NOT_FOUND, "Событие не найдено")

    # Check if user is owner
    cam = await db.get(Cam, event.cam_id)
    if cam is None:
        raise AppError(OBJECT_NOT_FOUND, "Камера не найдена")

    if payload.role != "admin" and cam.ownuser != payload.uid:
        raise AppError(FORBIDDEN_RESOURCE, "Только владелец камеры может удалять события")

    # Delete photo from filesystem
    photo_path = os.path.join(
        settings.motion_files_path,
        str(event.cam_id),
        event.photo_filename
    )

    if os.path.exists(photo_path):
        try:
            os.remove(photo_path)
        except Exception:
            pass  # Ignore errors, delete from DB anyway

    # Delete from database
    await db.delete(event)
    await db.commit()

    return deleted("motion_event", {"id": event_id})


@router.get(
    "/cams/{cam_id}/motion/status",
    dependencies=[Depends(rate_limit("motion_read", capacity=60, per_minute=60))],
)
async def get_motion_detection_status(
    cam_id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Get motion detection status for a camera."""
    await _check_cam_access(db, cam_id, payload.uid, payload.role == "admin")

    is_active = motion_detection.is_motion_detection_active(cam_id)

    return single("motion", {
        "cam_id": cam_id,
        "is_active": is_active,
    })


@router.post(
    "/cams/{cam_id}/motion/start",
    dependencies=[Depends(rate_limit("motion_control", capacity=10, per_minute=10))],
)
async def start_motion_detection_endpoint(
    cam_id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Start motion detection for a camera (owner only)."""
    cam = await _check_cam_access(db, cam_id, payload.uid, payload.role == "admin")

    # Check if user is owner
    if payload.role != "admin" and cam.ownuser != payload.uid:
        raise AppError(FORBIDDEN_RESOURCE, "Только владелец может управлять motion detection")

    # Check if owner has motion_detection_allowed
    from app.models.user import User
    owner = await db.get(User, cam.ownuser)
    if not owner or owner.motion_detection_allowed != 1:
        raise AppError(FORBIDDEN_RESOURCE, "Motion detection не разрешен для этого пользователя")

    success = await motion_detection.start_motion_detection(cam_id)

    return single("motion", {
        "cam_id": cam_id,
        "started": success,
        "is_active": motion_detection.is_motion_detection_active(cam_id),
    })


@router.post(
    "/cams/{cam_id}/motion/stop",
    dependencies=[Depends(rate_limit("motion_control", capacity=10, per_minute=10))],
)
async def stop_motion_detection_endpoint(
    cam_id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Stop motion detection for a camera (owner only)."""
    cam = await _check_cam_access(db, cam_id, payload.uid, payload.role == "admin")

    # Check if user is owner
    if payload.role != "admin" and cam.ownuser != payload.uid:
        raise AppError(FORBIDDEN_RESOURCE, "Только владелец может управлять motion detection")

    success = await motion_detection.stop_motion_detection(cam_id)

    return single("motion", {
        "cam_id": cam_id,
        "stopped": success,
        "is_active": motion_detection.is_motion_detection_active(cam_id),
    })
