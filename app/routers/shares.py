import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import FORBIDDEN_RESOURCE, OBJECT_NOT_FOUND, SHARE_INVALID
from app.core.config import settings
from app.core.errors import AppError
from app.core.introspection import supports
from app.core.response import bulk, bulk_listing, created, deleted, failed_item, listing, single
from app.core.rate_limit import rate_limit
from app.core.security import TokenPayload, create_share_token
from app.db.session import get_db
from app.deps.auth import require_access
from app.deps.ids import parse_ids
from app.deps.list_query import ListQuery, list_query
from app.deps.request_body import BulkBody, SingleBody, bulk_body, read_envelope, single_body
from app.models.cams import Cam
from app.models.switch import Switch
from app.models.sharing import Sharing
from app.schemas.sharing import SharingCreate, SharingCreateOut, SharingOut
from app.services import recordings

router = APIRouter(prefix="/v1/shares", tags=["shares"])

MAX_BULK = 100


async def _share_create_body(request: Request) -> SingleBody[SharingCreate]:
    # `partial` is a bulk capability; keep it out of this single-object POST's
    # OPTIONS declaration while retaining the shared v1.2 request parser.
    return await single_body("share", SharingCreate)(request)


async def _optional_partial_body(request: Request) -> bool:
    _, options = await read_envelope(request, optional=True)
    return options.partial_allowed


async def _optional_delete_body(request: Request) -> None:
    await read_envelope(request, optional=True)

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


async def _build_share(body: SharingCreate, payload: TokenPayload, db: AsyncSession) -> tuple[Sharing, int, int]:
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

    if body.device_type == "cams" and body.type == "record" and device.connection_resource_id is not None:
        raise AppError(SHARE_INVALID, "Локальные записи недоступны для облачной камеры")

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
    return Sharing(
        uid=payload.uid,
        type=body.type,
        device_type=body.device_type,
        device_id=body.device_id,
        filename=body.filename,
        added_at=now,
        updated_at=now,
    ), ttl, now


def _share_create_out(sharing: Sharing, ttl: int, now: int, uid: int) -> dict:
    token = create_share_token(
        sha_id=sharing.id,
        user_id=uid,
        type=sharing.type,
        device_type=sharing.device_type,
        device_id=sharing.device_id,
        filename=sharing.filename,
        ttl_seconds=ttl,
    )
    return SharingCreateOut(
        id=sharing.id,
        type=sharing.type,
        device_type=sharing.device_type,
        device_id=sharing.device_id,
        filename=sharing.filename,
        token=token,
        expires_at=now + ttl,
    ).model_dump()

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


@router.get("/bulk/{ids}", dependencies=[Depends(rate_limit("shares_read", capacity=60, per_minute=60))])
@supports("bulk", max_bulk=MAX_BULK)
async def get_shares_bulk(
    ids: str,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    id_list = parse_ids(ids, MAX_BULK)
    rows = []
    for sharing_id in id_list:
        sharing = await _get_visible_share(sharing_id, payload, db)
        rows.append(_to_out(sharing))
    return bulk_listing("shares", rows, requested=len(id_list))

@router.post("", dependencies=[Depends(rate_limit("shares_write", capacity=20, per_minute=20))])
async def create_share(
    body_: SingleBody[SharingCreate] = Depends(_share_create_body),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    sharing, ttl, now = await _build_share(body_.obj, payload, db)
    db.add(sharing)
    await db.commit()
    return created("share", _share_create_out(sharing, ttl, now, payload.uid))


@router.post("/bulk", dependencies=[Depends(rate_limit("shares_write", capacity=20, per_minute=20))])
async def create_shares_bulk(
    body: BulkBody[SharingCreate] = Depends(bulk_body("shares", SharingCreate, max_items=MAX_BULK)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    partial = body.partial_allowed
    succeeded: list[dict] = []
    failed: list[dict] = body.invalid()
    pending: list[tuple[Sharing, int, int]] = []

    for index, item in body.valid():
        try:
            sharing, ttl, now = await _build_share(item, payload, db)
            if partial:
                db.add(sharing)
                await db.commit()
                succeeded.append(_share_create_out(sharing, ttl, now, payload.uid))
            else:
                pending.append((sharing, ttl, now))
        except AppError as exc:
            if not partial:
                await db.rollback()
                raise
            await db.rollback()
            failed.append(failed_item(index, exc))

    if not partial:
        db.add_all([sharing for sharing, _, _ in pending])
        await db.commit()
        succeeded = [_share_create_out(sharing, ttl, now, payload.uid) for sharing, ttl, now in pending]

    failed.sort(key=lambda entry: entry["id"])
    return bulk("shares", succeeded, failed, op="create", partial=partial)

@router.delete("/{id}", dependencies=[Depends(rate_limit("shares_write", capacity=20, per_minute=20))])
async def delete_share(
    id: int,
    _body: None = Depends(_optional_delete_body),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    sharing = await _get_visible_share(id, payload, db)
    await db.delete(sharing)
    await db.commit()

    return deleted("share", {"id": id})


@router.delete("/bulk/{ids}", dependencies=[Depends(rate_limit("shares_write", capacity=20, per_minute=20))])
@supports("bulk", "partial", max_bulk=MAX_BULK)
async def delete_shares_bulk(
    ids: str,
    partial_allowed: bool = Depends(_optional_partial_body),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    id_list = parse_ids(ids, MAX_BULK)
    succeeded: list[dict] = []
    failed: list[dict] = []
    for sharing_id in id_list:
        try:
            sharing = await _get_visible_share(sharing_id, payload, db)
            await db.delete(sharing)
            succeeded.append({"id": sharing_id})
            if partial_allowed:
                await db.commit()
        except AppError as exc:
            await db.rollback()
            if not partial_allowed:
                raise
            failed.append(failed_item(sharing_id, exc))
    if not partial_allowed or succeeded:
        await db.commit()
    return bulk(
        "shares", succeeded, failed, op="delete", partial=partial_allowed,
        all_failed_code=failed[0].code if failed else None,
    )
