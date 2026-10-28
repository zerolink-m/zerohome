import time

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import FORBIDDEN_RESOURCE, OBJECT_NOT_FOUND
from app.core.config import settings
from app.core.errors import AppError
from app.core.rate_limit import rate_limit
from app.core.response import bulk, bulk_listing, created, deleted, edited, failed_item, listing, single
from app.core.security import TokenPayload
from app.core.validation import field_error
from app.db.session import get_db
from app.deps.auth import require_access
from app.deps.ids import parse_ids, partial_query
from app.deps.list_query import ListQuery, list_query
from app.deps.request_body import BulkBody, SingleBody, bulk_body, single_body
from app.models.cams import Cam
from app.models.invitation import Invitation
from app.models.sharing import Sharing
from app.schemas.cams import CamCreate, CamPatch
from app.services import hls_manager, onvif_client, push, stream_manager
from app.services.ingest import lifecycle as ingest

from app.routers.cams._common import (
    BULK_MAX,
    CAM_LIST_COLUMNS,
    CONTROL_FIELDS,
    _can_manage,
    _get_visible_cam,
    _serialize,
    _visibility_where,
    router,
)

_cams_query = list_query(sortable=CAM_LIST_COLUMNS, searchable=CAM_LIST_COLUMNS)

_FIELD_MAPPING = {
    "protocol": "proto",
    "address": "addr",
    "control_protocol": "control_proto",
    "control_address": "control_addr",
}

_READ = Depends(rate_limit("cams_read", capacity=60, per_minute=60))
_WRITE = Depends(rate_limit("cams_write", capacity=20, per_minute=20))


def _new_cam(body: CamCreate, uid: int, now: int) -> Cam:
    return Cam(
        ownuser=uid,
        name=body.name if body.name is not None else "",
        enabled=body.enabled if body.enabled is not None else 0,
        notify=body.notify if body.notify is not None else 0,
        proto=body.protocol,
        addr=body.address,
        port=body.port,
        path=body.path if body.path is not None else "",
        username=body.username,
        password=body.password,
        control_proto=body.control_protocol,
        control_addr=body.control_address,
        control_port=body.control_port,
        control_username=body.control_username,
        control_password=body.control_password,
        added_at=now,
        updated_at=now,
    )


def _apply_patch(cam: Cam, body: CamPatch) -> tuple[bool, bool, bool]:
    """Set the explicitly passed fields. -> (changed, control_touched, notify_enabled)."""
    data = body.model_dump()
    fields = {key: data[key] for key in body.model_fields_set}

    control_touched = any(f in fields for f in CONTROL_FIELDS)
    notify_enabled = fields.get("notify") == 1 and cam.notify != 1
    changed = False
    for key, value in fields.items():
        db_field = _FIELD_MAPPING.get(key, key)
        if getattr(cam, db_field) != value:
            changed = True
        setattr(cam, db_field, value)
    if fields:
        cam.updated_at = int(time.time())
    return changed, control_touched, notify_enabled


def _after_patch(cam: Cam, control_touched: bool, notify_enabled: bool) -> None:
    if control_touched:
        onvif_client.invalidate(cam.id)
    if notify_enabled:
        push.notify_cam_background(cam.id, f"Push включён для камеры {cam.name or cam.id}")


async def _delete_cam(cam: Cam, db: AsyncSession) -> None:
    await stream_manager.stop(cam.id)
    if settings.HLS_ENABLED:
        await hls_manager.stop(cam.id)
    onvif_client.invalidate(cam.id)
    await db.execute(
        Invitation.__table__.delete().where(Invitation.device_type == "cams", Invitation.device_id == cam.id)
    )
    await db.execute(
        Sharing.__table__.delete().where(Sharing.device_type == "cams", Sharing.device_id == cam.id)
    )
    await db.delete(cam)


async def _visible_by_ids(ids: list[int], payload: TokenPayload, db: AsyncSession) -> dict[int, Cam]:
    stmt = select(Cam).where(Cam.id.in_(ids))
    condition = _visibility_where(payload)
    if condition is not None:
        stmt = stmt.where(condition)
    return {c.id: c for c in (await db.execute(stmt)).scalars().all()}


def _bulk_reject(failures: list[tuple], partial: bool):
    """Atomic mode: the first failure rejects the whole request (nothing applied)."""
    if failures and not partial:
        _, code, msg = failures[0]
        raise AppError(code, msg)


def _failed(failures: list[tuple]) -> list[dict]:
    return [failed_item(i, msg) for i, _, msg in failures]


# --- single -------------------------------------------------------------------

@router.get("", dependencies=[_READ])
async def list_cams(
    q: ListQuery = Depends(_cams_query),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Cam)
    condition = _visibility_where(payload)
    if condition is not None:
        stmt = stmt.where(condition)
    if not q.sort_by:
        stmt = stmt.order_by(Cam.id)

    rows, total = await q.paginate(db, stmt)
    return listing("cams", [_serialize(cam, payload) for cam in rows], query=q, total=total)


# --- bulk (declared before /{id} routes) -----------------------------------------

@router.post("/bulk", dependencies=[_WRITE])
async def create_cams(
    body: BulkBody[CamCreate] = Depends(bulk_body("cams", CamCreate, max_items=BULK_MAX)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    now = int(time.time())
    made = [(idx, _new_cam(obj, payload.uid, now)) for idx, obj in body.valid()]
    for _, cam in made:
        db.add(cam)
    await db.commit()
    for _, cam in made:
        await db.refresh(cam)
        await ingest.on_cam_saved(cam)
    return bulk(
        "cams", [_serialize(cam, payload) for _, cam in made], body.invalid(),
        op="create", partial=body.partial_allowed,
    )


@router.get("/bulk/{ids}", dependencies=[_READ])
async def get_cams_bulk(
    ids: str,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    id_list = parse_ids(ids, BULK_MAX)
    found = await _visible_by_ids(id_list, payload, db)
    if any(i not in found for i in id_list):
        # missing and invisible ids are indistinguishable: atomic 082, nothing revealed
        raise AppError(OBJECT_NOT_FOUND, "Camera not found")
    return bulk_listing("cams", [_serialize(found[i], payload) for i in id_list],
                        requested=len(id_list))


@router.patch("/bulk/{ids}", dependencies=[_WRITE])
async def patch_cams_bulk(
    ids: str,
    body: BulkBody[CamPatch] = Depends(bulk_body("cams", CamPatch, max_items=BULK_MAX)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """data.cams[i] is applied to ids[i] (same length and order)."""
    id_list = parse_ids(ids, BULK_MAX)
    if len(body.items) != len(id_list):
        raise AppError("109", "Some fields are invalid", fields={
            "cams": field_error("length must match the number of ids", type="array",
                                max_length=len(id_list), actual_length=len(body.items))})
    found = await _visible_by_ids(id_list, payload, db)

    failures: list[tuple] = []  # (cam id, code, message)
    work: list[tuple[Cam, CamPatch]] = []
    for cam_id, item in zip(id_list, body.items):
        cam = found.get(cam_id)
        if item.error is not None:
            failures.append((cam_id, "109", item.error))
        elif cam is None:
            failures.append((cam_id, OBJECT_NOT_FOUND, "Camera not found"))
        elif not _can_manage(cam, payload):
            failures.append((cam_id, FORBIDDEN_RESOURCE, "Изменение камеры доступно только владельцу или admin"))
        else:
            work.append((cam, item.obj))
    _bulk_reject(failures, body.partial_allowed)

    effects = []
    results = []
    for cam, patch in work:
        _, control_touched, notify_enabled = _apply_patch(cam, patch)
        effects.append((cam, control_touched, notify_enabled, set(patch.model_fields_set)))
        results.append(cam)
    await db.commit()
    for cam, control_touched, notify_enabled, touched in effects:
        _after_patch(cam, control_touched, notify_enabled)
        await ingest.on_cam_saved(cam, touched)
    return bulk("cams", [_serialize(c, payload) for c in results], _failed(failures), op="edit",
                partial=body.partial_allowed, all_failed_code=failures[0][1] if failures else "109")


@router.delete("/bulk/{ids}", dependencies=[_WRITE])
async def delete_cams_bulk(
    ids: str,
    partial_allowed: bool = Depends(partial_query),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """DELETE has an optional body; the flag may come in `?partial_allowed=` (or options)."""
    id_list = parse_ids(ids, BULK_MAX)
    found = await _visible_by_ids(id_list, payload, db)

    failures: list[tuple] = []
    work: list[Cam] = []
    for cam_id in id_list:
        cam = found.get(cam_id)
        if cam is None:
            failures.append((cam_id, OBJECT_NOT_FOUND, "Camera not found"))
        elif not _can_manage(cam, payload):
            failures.append((cam_id, FORBIDDEN_RESOURCE, "Удаление камеры доступно только владельцу или admin"))
        else:
            work.append(cam)
    _bulk_reject(failures, partial_allowed)

    for cam in work:
        await _delete_cam(cam, db)
    await db.commit()
    for cam in work:
        await ingest.on_cam_deleted(cam.id)
    return bulk("cams", [{"id": c.id} for c in work], _failed(failures), op="delete", partial=partial_allowed,
                all_failed_code=failures[0][1] if failures else "109")


# --- single (by id) ---------------------------------------------------------------

@router.get("/{id}", dependencies=[_READ])
async def get_cam(id: int, payload: TokenPayload = Depends(require_access), db: AsyncSession = Depends(get_db)):
    cam = await _get_visible_cam(id, payload, db)
    return single("cam", _serialize(cam, payload))


@router.post("", dependencies=[_WRITE])
async def create_cam(
    body: SingleBody[CamCreate] = Depends(single_body("cam", CamCreate)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    cam = _new_cam(body.obj, payload.uid, int(time.time()))
    db.add(cam)
    await db.commit()
    await db.refresh(cam)
    await ingest.on_cam_saved(cam)
    return created("cam", _serialize(cam, payload))


@router.patch("/{id}", dependencies=[_WRITE])
async def patch_cam(
    id: int,
    body: SingleBody[CamPatch] = Depends(single_body("cam", CamPatch)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    cam = await _get_visible_cam(id, payload, db)
    if not _can_manage(cam, payload):
        raise AppError(FORBIDDEN_RESOURCE, "Изменение камеры доступно только владельцу или admin")

    changed, control_touched, notify_enabled = _apply_patch(cam, body.obj)
    await db.commit()
    _after_patch(cam, control_touched, notify_enabled)
    await ingest.on_cam_saved(cam, set(body.obj.model_fields_set))
    return edited("cam", _serialize(cam, payload), changed=changed)


@router.delete("/{id}", dependencies=[_WRITE])
async def delete_cam(
    id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    cam = await _get_visible_cam(id, payload, db)
    if not _can_manage(cam, payload):
        raise AppError(FORBIDDEN_RESOURCE, "Удаление камеры доступно только владельцу или admin")

    await _delete_cam(cam, db)
    await db.commit()
    await ingest.on_cam_deleted(id)
    return deleted("cam", {"id": id})


# --- stream / ptz / recordings ------------------------------------------------
