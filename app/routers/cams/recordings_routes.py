import time

from fastapi import Depends, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import Integer, String, column
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import FORBIDDEN_RESOURCE, INVALID_QUERY, RECORDING_NOT_ALLOWED, SERVER_ERROR, VALIDATION_ERROR
from app.core.errors import AppError
from app.core.introspection import declare, supports
from app.core.rate_limit import PointBudget, rate_limit
from app.core.response import edited, listing, single
from app.core.security import TokenPayload
from app.core.validation import field_error
from app.db.session import get_db
from app.deps.auth import AccessContext, require_access, resolve_record_access
from app.deps.list_query import ListQuery, list_query
from app.deps.request_body import SingleBody
from app.models.user import User
from app.schemas.cams import CamRecordingOut, CamRecordingPatch
from app.schemas.recording import RecordingOut
from app.services import recordings, snapshot

from app.routers.cams._common import (
    _LONG_CACHE_HEADERS,
    _NO_CACHE_HEADERS,
    _can_manage,
    _get_cam_for_access,
    _get_visible_cam,
    single_body_for_route,
    router,
)


def _require_local_recordings(cam) -> None:
    if cam.connection_resource_id is not None:
        raise AppError(VALIDATION_ERROR, "Локальная запись недоступна для облачной камеры")

# recordings live on the filesystem, so the columns are virtual (typed for the query parser only)
_REC_COLUMNS = {
    "id": column("id", String),
    "filename": column("filename", String),
    "format": column("format", String),
    "started_at": column("started_at", Integer),
    "size_bytes": column("size_bytes", Integer),
}
_recordings_query = list_query(
    sortable=_REC_COLUMNS, searchable=_REC_COLUMNS,
    default_limit=25, max_limit=255,
)


def _match(item: dict, cond) -> bool:
    raw = item[cond.column]
    if cond.method in ("equals", "exactly"):
        if isinstance(raw, int):
            try:
                return raw == int(cond.value)
            except ValueError:
                raise AppError(INVALID_QUERY, "Invalid query parameters", query={
                    "search": field_error("must be an integer for this column", type="int", value=cond.value)})
        return str(raw).casefold() == cond.value.casefold()
    s, v = str(raw).casefold(), cond.value.casefold()
    if cond.method == "include":
        return v in s
    if cond.method == "starts":
        return s.startswith(v)
    if cond.method == "ends":
        return s.endswith(v)
    return v not in s  # exclude


def _filter(items: list[dict], q: ListQuery) -> list[dict]:
    """Apply v1.2 simple-search conditions, which are always combined with AND."""
    if not q.conditions:
        return items
    out = []
    for it in items:
        if all(_match(it, condition) for condition in q.conditions):
            out.append(it)
    return out


def _sort(items: list[dict], q: ListQuery, default_direction: str = "desc") -> list[dict]:
    if not q.sort_by:  # default: newest first (historical behaviour)
        return sorted(items, key=lambda i: i["started_at"], reverse=default_direction == "desc")
    items = list(items)
    # stable multi-key sort: apply the least significant key first
    for idx in range(len(q.sort_by) - 1, -1, -1):
        name = q.sort_by[idx]
        direction = q.order[idx] if len(q.order) > 1 else q.order[0]
        items.sort(key=lambda i, n=name: i[n], reverse=direction == "desc")
    return items


def _int_query(name: str, raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        raise AppError(INVALID_QUERY, "Invalid query parameters", query={
            name: field_error("must be an integer", type="int", actual_type="string", value=raw)})


@router.get("/{id}/recording", dependencies=[Depends(rate_limit("cams_read", capacity=60, per_minute=60))])
async def get_cam_recording(
    id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    cam = await _get_visible_cam(id, payload, db)
    if not _can_manage(cam, payload):
        raise AppError(FORBIDDEN_RESOURCE, "Настройки записи доступны только владельцу или admin")
    _require_local_recordings(cam)

    return single(
        "recording_config",
        CamRecordingOut(
            record=cam.record or 0,
            record_type=cam.record_type,
            record_metadata=cam.record_meta,
        ).model_dump(),
    )


@router.patch("/{id}/recording", dependencies=[Depends(rate_limit("cams_write", capacity=20, per_minute=20))])
async def patch_cam_recording(
    id: int,
    body: SingleBody[CamRecordingPatch] = Depends(single_body_for_route("recording_config", CamRecordingPatch)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    cam = await _get_visible_cam(id, payload, db)
    if not _can_manage(cam, payload):
        raise AppError(FORBIDDEN_RESOURCE, "Настройки записи доступны только владельцу или admin")
    _require_local_recordings(cam)

    user = await db.get(User, payload.uid)
    if user is None or user.allowrecord != 1:
        raise AppError(RECORDING_NOT_ALLOWED, "У пользователя не включён флаг allowrecord")

    fields = body.obj.model_dump(exclude_unset=True)
    record = fields.get("record", cam.record)
    record_type = fields.get("record_type", cam.record_type)
    record_metadata = fields.get("record_metadata", cam.record_meta)

    if record == 1:
        if record_type is None:
            raise AppError(VALIDATION_ERROR, "Some fields are invalid", fields={
                "record_type": field_error("is required when record is 1", required=True, type="string")})
        recordings.validate_record_meta(record_type, record_metadata)

    changed = False
    for key, value in fields.items():
        if key == "record_metadata":
            changed |= cam.record_meta != value
            cam.record_meta = value
        else:
            changed |= getattr(cam, key) != value
            setattr(cam, key, value)
    if fields:
        cam.updated_at = int(time.time())

    await db.commit()

    return edited(
        "recording_config",
        CamRecordingOut(
            record=cam.record or 0,
            record_type=cam.record_type,
            record_metadata=cam.record_meta,
        ).model_dump(),
        changed=changed,
    )


@router.get("/{id}/recordings", dependencies=[Depends(rate_limit("cams_read", capacity=60, per_minute=60))])
async def list_cam_recordings(
    id: int,
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    q: ListQuery = Depends(_recordings_query),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Standard list query (limit<=100, default 10; offset; sort_by; order; multi-search over
    id/filename/format/started_at/size_bytes) + custom time filters `from`/`to` (unix seconds)."""
    cam = await _get_visible_cam(id, payload, db)
    _require_local_recordings(cam)
    from_ts, to_ts = _int_query("from", from_), _int_query("to", to)

    all_items, _ = recordings.list_recordings(cam, 0, 10**9, from_ts, to_ts)
    default_direction = q.order[0] if request.query_params.get("order") else "desc"
    matched = _sort(_filter(all_items, q), q, default_direction)
    page = matched[q.offset: q.offset + q.limit]
    settings = q.settings(len(matched))
    if not q.sort_by:
        settings["order"] = default_direction
    return listing(
        "recordings", [RecordingOut(**item).model_dump() for item in page],
        settings=settings, total=len(matched),
    )


declare(list_cam_recordings, "x-time_range")


@router.get(
    "/{id}/recordings/{recording_id}",
    dependencies=[Depends(rate_limit("cams_read", capacity=60, per_minute=60))],
)
async def get_cam_recording_meta(
    id: int,
    recording_id: str,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    cam = await _get_visible_cam(id, payload, db)
    _require_local_recordings(cam)
    meta = recordings.get_recording(cam, recording_id)
    return single("recording", RecordingOut(**meta).model_dump())


@router.get(
    "/{id}/recordings/{recording_id}/file",
    dependencies=[Depends(rate_limit("cams_recording_file", capacity=20, per_minute=20))],
)
@supports("x-file")
async def get_cam_recording_file(
    id: int,
    recording_id: str,
    ctx: AccessContext = Depends(resolve_record_access),
    db: AsyncSession = Depends(get_db),
):
    cam = await _get_cam_for_access(id, ctx, db)
    _require_local_recordings(cam)
    full_path, media_type = recordings.resolve_file(cam, recording_id)

    headers = _LONG_CACHE_HEADERS if await snapshot.is_valid_video(full_path) else None
    return FileResponse(full_path, media_type=media_type, filename=recording_id, headers=headers)


SNAPSHOT_BUDGET = PointBudget("cams_recording_snapshot", capacity=3000, per_minute=3000)
SNAPSHOT_CACHED_COST = 1
SNAPSHOT_GENERATE_COST = 3


@router.get("/{id}/recordings/{recording_id}/snapshot")
@supports("x-snapshot")
async def get_cam_recording_snapshot(
    id: int,
    recording_id: str,
    request: Request,
    at: float | None = Query(default=None, ge=0),
    ctx: AccessContext = Depends(resolve_record_access),
    db: AsyncSession = Depends(get_db),
):
    cam = await _get_cam_for_access(id, ctx, db)
    _require_local_recordings(cam)
    full_path, _ = recordings.resolve_file(cam, recording_id)

    cost = SNAPSHOT_CACHED_COST if snapshot.is_cached(cam.id, recording_id) else SNAPSHOT_GENERATE_COST
    await SNAPSHOT_BUDGET.charge(request, cost)

    try:
        data = await snapshot.get_cached_snapshot(cam.id, recording_id, full_path, at)
    except snapshot.SnapshotError as exc:
        raise AppError(SERVER_ERROR, str(exc), headers=_NO_CACHE_HEADERS)

    return Response(content=data, media_type="image/jpeg", headers=_LONG_CACHE_HEADERS)


# --- HLS streaming ------------------------------------------------------------
