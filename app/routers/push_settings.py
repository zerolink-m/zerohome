import time

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import FORBIDDEN_RESOURCE, OBJECT_NOT_FOUND
from app.core.errors import AppError
from app.core.response import edited, single
from app.core.rate_limit import rate_limit
from app.core.security import TokenPayload
from app.db.session import get_db
from app.deps.auth import require_access
from app.deps.request_body import SingleBody, single_body
from app.models.cams import Cam
from app.models.push_settings import PushSettings
from app.models.switch import Switch
from app.schemas.push_settings import PushSettingsResponse, PushSettingsUpdate
from app.services import motion_lifecycle

router = APIRouter(prefix="/v1", tags=["push_settings"])


def _out(settings: PushSettings) -> dict:
    return PushSettingsResponse.model_validate(settings).model_dump()


async def _apply_update(db: AsyncSession, settings: PushSettings, upd: PushSettingsUpdate):
    update_data = upd.model_dump(exclude_unset=True)
    changed = any(getattr(settings, f) != v for f, v in update_data.items())
    if changed:
        for field, value in update_data.items():
            setattr(settings, field, value)
        settings.updated_at = int(time.time())
        await db.commit()
        await db.refresh(settings)
    return edited("push_settings", _out(settings), changed=changed)


async def _get_or_create_push_settings(
    db: AsyncSession,
    user_id: int,
    device_type: str,
    device_id: int,
) -> PushSettings:
    """Get existing or create default push_settings."""
    stmt = select(PushSettings).where(
        PushSettings.user_id == user_id,
        PushSettings.device_type == device_type,
        PushSettings.device_id == device_id,
    )
    result = await db.execute(stmt)
    settings = result.scalar_one_or_none()

    if settings is None:
        # Create default settings
        now = int(time.time())
        settings = PushSettings(
            user_id=user_id,
            device_type=device_type,
            device_id=device_id,
            enabled=1,
            created_at=now,
            updated_at=now,
        )
        db.add(settings)
        await db.commit()
        await db.refresh(settings)

    return settings


async def _check_device_access(
    db: AsyncSession,
    user_id: int,
    device_type: str,
    device_id: int,
    is_admin: bool,
) -> None:
    """Check if user has access to device."""
    # Invitees intentionally retain access to push settings while a device is disabled.
    if device_type == "cams":
        cam = await db.get(Cam, device_id)
        if cam is None:
            raise AppError(OBJECT_NOT_FOUND, "Камера не найдена")

        if not is_admin and cam.ownuser != user_id:
            # Check if user has invitation
            from app.models.invitation import Invitation
            stmt = select(Invitation).where(
                Invitation.device_type == "cams",
                Invitation.device_id == device_id,
                Invitation.status == "accepted",
            )
            result = await db.execute(stmt)
            invitations = result.scalars().all()

            # Get user email
            from app.models.user import User
            user = await db.get(User, user_id)
            if user is None or not any(inv.email.lower() == user.email.lower() for inv in invitations):
                raise AppError(FORBIDDEN_RESOURCE, "Нет доступа к этой камере")

    elif device_type == "switches":
        switch = await db.get(Switch, device_id)
        if switch is None:
            raise AppError(OBJECT_NOT_FOUND, "Устройство не найдено")

        if not is_admin and switch.ownuser != user_id:
            # Check if user has invitation
            from app.models.invitation import Invitation
            stmt = select(Invitation).where(
                Invitation.device_type == "switches",
                Invitation.device_id == device_id,
                Invitation.status == "accepted",
            )
            result = await db.execute(stmt)
            invitations = result.scalars().all()

            # Get user email
            from app.models.user import User
            user = await db.get(User, user_id)
            if user is None or not any(inv.email.lower() == user.email.lower() for inv in invitations):
                raise AppError(FORBIDDEN_RESOURCE, "Нет доступа к этому устройству")


@router.get(
    "/cams/{cam_id}/push-settings",
    dependencies=[Depends(rate_limit("push_settings_read", capacity=60, per_minute=60))],
)
async def get_cam_push_settings(
    cam_id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Get push settings for camera."""
    await _check_device_access(db, payload.uid, "cams", cam_id, payload.role == "admin")
    settings = await _get_or_create_push_settings(db, payload.uid, "cams", cam_id)
    return single("push_settings", _out(settings))


@router.patch(
    "/cams/{cam_id}/push-settings",
    dependencies=[Depends(rate_limit("push_settings_write", capacity=20, per_minute=20))],
)
async def update_cam_push_settings(
    cam_id: int,
    body: SingleBody[PushSettingsUpdate] = Depends(single_body("push_settings", PushSettingsUpdate)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Update push settings for camera."""
    await _check_device_access(db, payload.uid, "cams", cam_id, payload.role == "admin")
    settings = await _get_or_create_push_settings(db, payload.uid, "cams", cam_id)

    updated = await _apply_update(db, settings, body.obj)
    if "motion_detection_enabled" in body.obj.model_fields_set:
        await motion_lifecycle.on_preferences_changed(cam_id)
    return updated


@router.get(
    "/switches/{switch_id}/push-settings",
    dependencies=[Depends(rate_limit("push_settings_read", capacity=60, per_minute=60))],
)
async def get_switch_push_settings(
    switch_id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Get push settings for switch."""
    await _check_device_access(db, payload.uid, "switches", switch_id, payload.role == "admin")
    settings = await _get_or_create_push_settings(db, payload.uid, "switches", switch_id)
    return single("push_settings", _out(settings))


@router.patch(
    "/switches/{switch_id}/push-settings",
    dependencies=[Depends(rate_limit("push_settings_write", capacity=20, per_minute=20))],
)
async def update_switch_push_settings(
    switch_id: int,
    body: SingleBody[PushSettingsUpdate] = Depends(single_body("push_settings", PushSettingsUpdate)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Update push settings for switch."""
    await _check_device_access(db, payload.uid, "switches", switch_id, payload.role == "admin")
    settings = await _get_or_create_push_settings(db, payload.uid, "switches", switch_id)

    # Update only provided fields
    return await _apply_update(db, settings, body.obj)
