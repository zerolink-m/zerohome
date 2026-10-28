import time

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import FORBIDDEN_RESOURCE, OBJECT_NOT_FOUND, SHARE_INVALID
from app.core.config import settings
from app.core.errors import AppError
from app.core.response import created, deleted, listing, single
from app.core.rate_limit import rate_limit
from app.core.security import TokenPayload, create_share_token
from app.db.session import get_db
from app.deps.auth import require_access
from app.deps.list_query import ListQuery, list_query
from app.deps.request_body import SingleBody, single_body
from app.models.cams import Cam
from app.models.switch import Switch
from app.models.sharing import Sharing
from app.schemas.sharing import SharingCreate, SharingCreateOut, SharingOut
from app.services import recordings

router = APIRouter(prefix="/v1/shares", tags=["shares"])

SHARES_FIELDS = ["id", "uid", "type", "device_type", "device_id", "added_at", "updated_at"]
shares_query = list_query(model=Sharing, sortable=SHARES_FIELDS, searchable=SHARES_FIELDS)

def _to_out(s: Sharing) -> dict:
    return SharingOut(
        id=s.id,
        uid=s.uid,
        type=s.type,
        device_type=s.device_type,
        device_id=s.device_id,
        filename=s.filename,
        updated_at=s.updated_at,
        added_at=s.added_at,
    ).model_dump()

async def _get_visible_share(id: int, payload: TokenPayload, db: AsyncSession) -> Sharing:
    sharing = await db.get(Sharing, id)
    if sharing is None:
        raise AppError(OBJECT_NOT_FOUND, "Ссылка не найдена")

    if payload.role != "admin" and sharing.uid != payload.uid:
        raise AppError(OBJECT_NOT_FOUND, "Ссылка не найдена")

    return sharing

@router.get("", dependencies=[Depends(rate_limit("shares_read", capacity=60, per_minute=60))])
async def list_shares(
    q: ListQuery = Depends(shares_query),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Sharing)
    if payload.role != "admin":
        stmt = stmt.where(Sharing.uid == payload.uid)

    rows, total = await q.paginate(db, stmt)
    return listing("shares", [_to_out(s) for s in rows], query=q, total=total)

@router.get("/{id}", dependencies=[Depends(rate_limit("shares_read", capacity=60, per_minute=60))])
async def get_share(
    id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    sharing = await _get_visible_share(id, payload, db)
    return single("share", _to_out(sharing))

@router.post("", dependencies=[Depends(rate_limit("shares_write", capacity=20, per_minute=20))])
async def create_share(
    body_: SingleBody[SharingCreate] = Depends(single_body("share", SharingCreate)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    body = body_.obj
    if body.device_type == "cams":
        device = await db.get(Cam, body.device_id)
        if body.type not in ("stream", "record"):
            raise AppError(SHARE_INVALID, "Для камер поддерживаются только type=stream и type=record")
    else:
        device = await db.get(Switch, body.device_id)
        if body.type != "device_use":
            raise AppError(SHARE_INVALID, "Для свитчей поддерживается только type=device_use")

    if device is None:
        raise AppError(OBJECT_NOT_FOUND, "Устройство не найдено")

    if payload.role != "admin" and device.ownuser != payload.uid:
        raise AppError(FORBIDDEN_RESOURCE, "Создавать ссылки может только владелец или admin")

    ttl = body.ttl_seconds if body.ttl_seconds is not None else settings.SHARE_TOKEN_MIN_TTL_SECONDS
    if ttl < settings.SHARE_TOKEN_MIN_TTL_SECONDS or ttl > settings.SHARE_TOKEN_MAX_TTL_SECONDS:
        raise AppError(
            SHARE_INVALID,
            f"ttl_seconds должен быть в диапазоне [{settings.SHARE_TOKEN_MIN_TTL_SECONDS}, "
            f"{settings.SHARE_TOKEN_MAX_TTL_SECONDS}]",
        )

    if body.type == "record" and body.device_type == "cams":
        recordings.resolve_file(device, body.filename)

    now = int(time.time())
    sharing = Sharing(
        uid=payload.uid,
        type=body.type,
        device_type=body.device_type,
        device_id=body.device_id,
        filename=body.filename,
        added_at=now,
        updated_at=now,
    )
    db.add(sharing)
    await db.commit()

    token = create_share_token(
        sha_id=sharing.id,
        user_id=payload.uid,
        type=sharing.type,
        device_type=sharing.device_type,
        device_id=sharing.device_id,
        filename=sharing.filename,
        ttl_seconds=ttl,
    )

    return created(
        "share",
        SharingCreateOut(
            id=sharing.id,
            type=sharing.type,
            device_type=sharing.device_type,
            device_id=sharing.device_id,
            filename=sharing.filename,
            token=token,
            expires_at=now + ttl,
        ).model_dump(),
    )

@router.delete("/{id}", dependencies=[Depends(rate_limit("shares_write", capacity=20, per_minute=20))])
async def delete_share(
    id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    sharing = await _get_visible_share(id, payload, db)
    await db.delete(sharing)
    await db.commit()

    return deleted("share", {"id": id})
