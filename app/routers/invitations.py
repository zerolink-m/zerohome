import time

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import FORBIDDEN_RESOURCE, INVITATION_INVALID, OBJECT_NOT_FOUND
from app.core.errors import AppError
from app.core.response import created, deleted, edited, listing, single
from app.core.rate_limit import rate_limit
from app.core.security import TokenPayload
from app.db.session import get_db
from app.deps.auth import require_access
from app.deps.list_query import ListQuery, list_query
from app.deps.request_body import SingleBody, single_body
from app.models.cams import Cam
from app.models.switch import Switch
from app.models.invitation import Invitation
from app.models.user import User
from app.schemas.invitation import InvitationCreate, InvitationOut

router = APIRouter(prefix="/v1/invitations", tags=["invitations"])

INVITATIONS_FIELDS = ["id", "device_type", "device_id", "email", "status", "invited_by", "added_at", "updated_at"]
invitations_query = list_query(model=Invitation, sortable=INVITATIONS_FIELDS, searchable=INVITATIONS_FIELDS)

def _to_out(i: Invitation) -> dict:
    return InvitationOut(
        id=i.id,
        device_type=i.device_type,
        device_id=i.device_id,
        email=i.email,
        invited_by=i.invited_by,
        status=i.status,
        added_at=i.added_at,
        updated_at=i.updated_at,
    ).model_dump()

async def _get_visible_invitation(id: int, payload: TokenPayload, db: AsyncSession) -> tuple[Invitation, User]:
    invitation = await db.get(Invitation, id)
    if invitation is None:
        raise AppError(OBJECT_NOT_FOUND, "Приглашение не найдено")

    user = await db.get(User, payload.uid)
    if user is None:
        raise AppError(OBJECT_NOT_FOUND, "Пользователь не найден")

    if payload.role != "admin":
        if invitation.invited_by != payload.uid and invitation.email.lower() != user.email.lower():
            raise AppError(OBJECT_NOT_FOUND, "Приглашение не найдено")

    return invitation, user

@router.get("", dependencies=[Depends(rate_limit("invitations_read", capacity=60, per_minute=60))])
async def list_invitations(
    q: ListQuery = Depends(invitations_query),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, payload.uid)
    if user is None:
        raise AppError(OBJECT_NOT_FOUND, "Пользователь не найден")

    stmt = select(Invitation)
    if payload.role != "admin":
        stmt = stmt.where(
            or_(
                Invitation.invited_by == payload.uid,
                func.lower(Invitation.email) == user.email.lower()
            )
        )

    rows, total = await q.paginate(db, stmt)
    return listing("invitations", [_to_out(i) for i in rows], query=q, total=total)

@router.get("/{id}", dependencies=[Depends(rate_limit("invitations_read", capacity=60, per_minute=60))])
async def get_invitation(
    id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    invitation, _ = await _get_visible_invitation(id, payload, db)
    return single("invitation", _to_out(invitation))

@router.post("", dependencies=[Depends(rate_limit("invitations_write", capacity=20, per_minute=20))])
async def create_invitation(
    body_: SingleBody[InvitationCreate] = Depends(single_body("invitation", InvitationCreate)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    body = body_.obj
    if body.device_type == "cams":
        device = await db.get(Cam, body.device_id)
    else:
        device = await db.get(Switch, body.device_id)

    if device is None:
        raise AppError(OBJECT_NOT_FOUND, "Устройство не найдено")

    if payload.role != "admin" and device.ownuser != payload.uid:
        raise AppError(FORBIDDEN_RESOURCE, "Приглашения может создавать только владелец или admin")

    owner = await db.get(User, device.ownuser)
    if owner is not None and owner.email.lower() == body.email.lower():
        raise AppError(INVITATION_INVALID, "Нельзя пригласить владельца устройства")

    existing = (
        await db.execute(
            select(Invitation).where(
                Invitation.device_type == body.device_type,
                Invitation.device_id == body.device_id,
                func.lower(Invitation.email) == body.email.lower(),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise AppError(INVITATION_INVALID, "Этот email уже приглашён на устройство")

    now = int(time.time())
    invitation = Invitation(
        device_type=body.device_type,
        device_id=body.device_id,
        email=body.email,
        invited_by=payload.uid,
        status="pending",
        added_at=now,
    )
    db.add(invitation)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppError(INVITATION_INVALID, "Этот email уже приглашён на устройство") from exc

    return created("invitation", _to_out(invitation))

@router.post("/{id}/accept", dependencies=[Depends(rate_limit("invitations_write", capacity=20, per_minute=20))])
async def accept_invitation(
    id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Custom action: the invitee (or admin) accepts the invitation."""
    invitation, user = await _get_visible_invitation(id, payload, db)

    if payload.role != "admin" and invitation.email.lower() != user.email.lower():
        raise AppError(FORBIDDEN_RESOURCE, "Принять приглашение может только приглашённый пользователь или admin")

    changed = invitation.status != "accepted"
    if changed:
        invitation.status = "accepted"
        invitation.updated_at = int(time.time())
        await db.commit()

    return edited("invitation", _to_out(invitation), changed=changed)

@router.post("/{id}/decline", dependencies=[Depends(rate_limit("invitations_write", capacity=20, per_minute=20))])
async def decline_invitation(
    id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Custom action: the invitee (or admin) declines the invitation (it is removed)."""
    invitation, user = await _get_visible_invitation(id, payload, db)

    if payload.role != "admin" and invitation.email.lower() != user.email.lower():
        raise AppError(FORBIDDEN_RESOURCE, "Отклонить приглашение может только приглашённый пользователь или admin")

    await db.delete(invitation)
    await db.commit()

    return deleted("invitation", {"id": id})

@router.delete("/{id}", dependencies=[Depends(rate_limit("invitations_write", capacity=20, per_minute=20))])
async def delete_invitation(
    id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    invitation, user = await _get_visible_invitation(id, payload, db)

    if payload.role != "admin" and invitation.invited_by != payload.uid and invitation.email.lower() != user.email.lower():
        raise AppError(FORBIDDEN_RESOURCE, "Удалять может только отправитель, получатель или admin")

    await db.delete(invitation)
    await db.commit()

    return deleted("invitation", {"id": id})
