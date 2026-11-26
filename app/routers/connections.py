"""CRUD for user-owned third-party cloud connections (currently only Tuya).

A Connection stores credentials for a provider's account; the sync worker
(app/services/connection_sync.py) periodically pulls that account's device
list and materialises the devices as `switches` rows with `added_by='auto'`.

The router surface is intentionally simple — one connection maps to one
provider account, so there's no notion of "test connection" beyond "did the
last sync succeed" (readable via `last_synced_at` / `last_sync_error`).

Security: `meta` fields are write-only. The output redaction (`redact_meta`)
strips secrets before returning to the client so screenshots / logs can't
leak them. The router refuses to return `access_secret` back to anyone,
including the owner — if you need to change it, PATCH a new one.
"""

import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import (
    BULK_LIMIT_EXCEEDED,
    CONNECTION_INVALID,
    FORBIDDEN_RESOURCE,
    OBJECT_NOT_FOUND,
    VALIDATION_ERROR,
)
from app.core.errors import AppError
from app.core.introspection import declare
from app.core.rate_limit import rate_limit
from app.core.response import bulk, bulk_listing, created, deleted, edited, failed_item, listing, single
from app.core.security import TokenPayload
from app.deps.ids import parse_ids
from app.core.validation import field_error
from app.db.session import get_db
from app.deps.auth import require_access
from app.deps.list_query import ListQuery, list_query
from app.deps.request_body import BulkBody, SingleBody, bulk_body, read_envelope, single_body
from app.models.connection import Connection, ConnectionCredentials
from app.models.cams import Cam
from app.models.connection import ConnectionResource
from app.models.invitation import Invitation
from app.models.sharing import Sharing
from app.models.switch import Switch
from app.schemas.connection import (
    ConnectionCreate,
    ConnectionOut,
    ConnectionPatch,
    _validate_tuya_meta,
    _validate_ufanet_meta,
    redact_meta,
)
from app.services import connection_sync, tuya_client_pool

router = APIRouter(prefix="/v1/connections", tags=["connections"])

CONNECTIONS_COLUMNS = {
    "id": Connection.id,
    "provider": Connection.provider,
    "name": Connection.name,
    "enabled": Connection.enabled,
    "added_at": Connection.added_at,
    "updated_at": Connection.updated_at,
}
connections_query = list_query(model=Connection, sortable=CONNECTIONS_COLUMNS, searchable=CONNECTIONS_COLUMNS)
MAX_BULK = 50


def _single_body(key, schema):
    """Single-object requests do not implement bulk partial execution.

    The shared body factory currently carries the ``partial`` declaration for
    bulk callers too, so clear it for this router's ordinary single-object
    methods before FastAPI introspects the dependency.
    """
    dependency = single_body(key, schema)
    dependency.cursed_functions = frozenset()
    dependency.cursed_meta = {}
    return dependency


def _visibility_where(payload: TokenPayload):
    """Admins see every connection; regular users only their own. Kept as a
    helper (mirrors the pattern in cams/switches routers) so future roles
    can be added without touching every query site."""
    if payload.role == "admin":
        return None
    return Connection.uid == payload.uid


def _can_manage(conn: Connection, payload: TokenPayload) -> bool:
    return payload.role == "admin" or conn.uid == payload.uid


def _serialize(conn: Connection) -> dict:
    return ConnectionOut(
        id=conn.id,
        uid=conn.uid,
        provider=conn.provider,
        name=conn.name,
        meta=redact_meta(conn.provider, conn.meta or {}),
        enabled=conn.enabled,
        status=conn.status,
        last_checked_at=conn.last_checked_at,
        last_synced_at=conn.last_synced_at,
        last_sync_error=conn.last_sync_error,
        added_at=conn.added_at,
        updated_at=conn.updated_at,
    ).model_dump()


async def _get_visible_connection(
    id: int, payload: TokenPayload, db: AsyncSession, *, for_update: bool = False
) -> Connection:
    stmt = select(Connection).where(Connection.id == id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    condition = _visibility_where(payload)
    if condition is not None:
        stmt = stmt.where(condition)
    conn = (await db.execute(stmt)).scalar_one_or_none()
    if conn is None:
        raise AppError(OBJECT_NOT_FOUND, "Соединение не найдено")
    return conn



async def _partial_option(request: Request):
    """Optional body for bulk DELETE: only options.partial_allowed / ?partial_allowed matter."""
    _, options = await read_envelope(request, optional=True)
    return options


declare(_partial_option, "bulk", "partial", max_bulk=MAX_BULK)


def _err(exc: AppError) -> dict:
    return {"error": exc.message}


def _new_connection(obj: ConnectionCreate, payload: TokenPayload, now: int) -> Connection:
    conn = Connection(
        uid=payload.uid,
        provider=obj.provider,
        name=obj.name,
        meta={"contract": obj.meta["contract"]} if obj.provider == "ufanet-smart-home" else obj.meta,
        enabled=obj.enabled,
        last_synced_at=None,
        last_sync_error=None,
        added_at=now,
        updated_at=now,
    )
    if obj.provider == "ufanet-smart-home":
        conn.credentials = ConnectionCredentials(credentials=dict(obj.meta), updated_at=now)
    return conn


async def _sync_quietly(conn: Connection, db: AsyncSession) -> None:
    """Kick off an immediate sync so the client sees devices on the next
    `GET /v1/switches` without waiting for the scheduled tick. Failures are
    recorded on the connection row itself (last_sync_error), not raised —
    a bad credential should still let the connection be created so the user
    can PATCH it and try again."""
    try:
        await connection_sync.sync_connection_now(conn.id)
    except Exception:
        pass
    await db.refresh(conn)


def _apply_patch(conn: Connection, fields: dict, payload: TokenPayload) -> tuple[bool, bool, dict]:
    """Permission/meta checks + mutation (no commit).
    Returns (changed, should_resync, old_meta)."""
    if not _can_manage(conn, payload):
        raise AppError(FORBIDDEN_RESOURCE, "Изменение соединения доступно только владельцу или admin")

    old_meta = dict(conn.meta or {})

    # meta patch is all-or-nothing: we don't merge on top of stored meta
    # because half-populating credentials would leave the connection in a
    # non-authenticatable state. Validate the new blob under the current
    # provider (provider itself is immutable — see ConnectionPatch).
    if fields.get("meta") is not None:
        # Keep the invariant "stored meta is always valid for its provider".
        try:
            if conn.provider == "tuya_cloud":
                _validate_tuya_meta(fields["meta"])
            elif conn.provider == "ufanet-smart-home":
                _validate_ufanet_meta(fields["meta"])
        except ValueError as exc:
            raise AppError(CONNECTION_INVALID, str(exc)) from exc

    changed = False
    should_resync = False
    for key, value in fields.items():
        if value is None:
            # explicit None means "not set" -> no-op
            continue
        if key == "meta" and conn.provider == "ufanet-smart-home":
            old_credentials = conn.credentials.credentials if conn.credentials else None
            if old_credentials != value:
                changed = True
                should_resync = True
                if conn.credentials is None:
                    conn.credentials = ConnectionCredentials(credentials=dict(value), updated_at=int(time.time()))
                else:
                    conn.credentials.credentials = dict(value)
                    conn.credentials.updated_at = int(time.time())
                conn.meta = {"contract": value["contract"]}
            continue
        if getattr(conn, key) != value:
            changed = True
            if key in ("meta", "enabled"):
                should_resync = True
        setattr(conn, key, value)
    if changed:
        conn.updated_at = int(time.time())
        if conn.provider == "ufanet-smart-home":
            conn.sync_revision += 1
            conn.status = "unknown"
            conn.last_checked_at = None
            conn.last_synced_at = None
            conn.last_sync_error = None
    return changed, should_resync, old_meta


async def _evict_pool(old_meta: dict, new_meta: dict | None) -> None:
    """Evict the pooled TuyaCloudClient keyed on the pre-patch credentials so a
    rotated access_secret (or changed endpoint) takes effect immediately
    instead of after the cached token's ~2h TTL. Also evict the new key in
    case only the secret changed (same access_id+endpoint key) — that key is
    unchanged but its client still holds the old secret."""
    if new_meta is None:
        return
    old_access_id = old_meta.get("access_id")
    old_endpoint = old_meta.get("endpoint")
    if isinstance(old_access_id, str) and isinstance(old_endpoint, str):
        await tuya_client_pool.invalidate(old_access_id, old_endpoint)
    new_access_id = new_meta.get("access_id")
    new_endpoint = new_meta.get("endpoint")
    if (
        isinstance(new_access_id, str)
        and isinstance(new_endpoint, str)
        and (new_access_id, new_endpoint) != (old_access_id, old_endpoint)
    ):
        await tuya_client_pool.invalidate(new_access_id, new_endpoint)


async def _delete_conn_row(conn: Connection, payload: TokenPayload, db: AsyncSession) -> tuple | None:
    """Permission checks + deletion (no commit). Returns (access_id, endpoint) for pool eviction."""
    if not _can_manage(conn, payload):
        raise AppError(FORBIDDEN_RESOURCE, "Удаление соединения доступно только владельцу или admin")

    del_meta = conn.meta or {}
    key = (del_meta.get("access_id"), del_meta.get("endpoint"))

    await _delete_connection_devices(conn, db)
    await db.delete(conn)
    return key


async def _delete_connection_devices(conn: Connection, db: AsyncSession) -> None:
    # Polymorphic invitations/sharings do not cascade with device deletion.
    auto_switch_ids = (
        await db.execute(
            select(Switch.id).where(
                Switch.connection_id == conn.id, Switch.added_by == "auto"
            )
        )
    ).scalars().all()
    if auto_switch_ids:
        await db.execute(
            Invitation.__table__.delete().where(
                Invitation.device_type == "switches",
                Invitation.device_id.in_(auto_switch_ids),
            )
        )
        await db.execute(
            Sharing.__table__.delete().where(
                Sharing.device_type == "switches",
                Sharing.device_id.in_(auto_switch_ids),
            )
        )

    camera_ids = (await db.execute(
        select(Cam.id).join(ConnectionResource, Cam.connection_resource_id == ConnectionResource.id)
        .where(ConnectionResource.connection_id == conn.id)
    )).scalars().all()
    if camera_ids:
        await db.execute(Invitation.__table__.delete().where(
            Invitation.device_type == "cams", Invitation.device_id.in_(camera_ids)))
        await db.execute(Sharing.__table__.delete().where(
            Sharing.device_type == "cams", Sharing.device_id.in_(camera_ids)))

    if auto_switch_ids and conn.provider == "ufanet-smart-home":
        await db.execute(Switch.__table__.delete().where(Switch.id.in_(auto_switch_ids)))
    if camera_ids:
        await db.execute(Cam.__table__.delete().where(Cam.id.in_(camera_ids)))
    await db.execute(ConnectionResource.__table__.delete().where(ConnectionResource.connection_id == conn.id))


async def _evict_deleted(key: tuple | None) -> None:
    # evict the cached client after the row is gone so we don't leak an httpx pool
    if key and isinstance(key[0], str) and isinstance(key[1], str):
        await tuya_client_pool.invalidate(key[0], key[1])


# --- list / get ----------------------------------------------------------------


@router.get("", dependencies=[Depends(rate_limit("connections_read", capacity=60, per_minute=60))])
async def list_connections(
    q: ListQuery = Depends(connections_query),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Connection)
    condition = _visibility_where(payload)
    if condition is not None:
        stmt = stmt.where(condition)

    rows, total = await q.paginate(db, stmt)
    return listing("connections", [_serialize(c) for c in rows], query=q, total=total)


@router.get(
    "/bulk/{ids}",
    dependencies=[Depends(rate_limit("connections_read", capacity=60, per_minute=60))],
)
async def get_connections_bulk(
    ids: str,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    id_list = parse_ids(ids, MAX_BULK)
    items = [_serialize(await _get_visible_connection(i, payload, db)) for i in id_list]
    return bulk_listing("connections", items, requested=len(id_list))


declare(get_connections_bulk, "bulk")


@router.get(
    "/{connection_id}",
    dependencies=[Depends(rate_limit("connections_read", capacity=60, per_minute=60))],
)
async def get_connection(
    connection_id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    conn = await _get_visible_connection(connection_id, payload, db)
    return single("connection", _serialize(conn))


# --- create --------------------------------------------------------------------


@router.post("", dependencies=[Depends(rate_limit("connections_write", capacity=10, per_minute=10))])
async def create_connection(
    body: SingleBody[ConnectionCreate] = Depends(_single_body("connection", ConnectionCreate)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    conn = _new_connection(body.obj, payload, int(time.time()))
    db.add(conn)
    await db.commit()

    if conn.enabled == 1:
        await _sync_quietly(conn, db)
    else:
        await db.refresh(conn)
    return created("connection", _serialize(conn))


@router.post("/bulk", dependencies=[Depends(rate_limit("connections_write", capacity=10, per_minute=10))])
async def create_connections_bulk(
    body: BulkBody[ConnectionCreate] = Depends(bulk_body("connections", ConnectionCreate, max_items=MAX_BULK)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    failed = body.invalid()
    rows = []
    now = int(time.time())
    for _, obj in body.valid():
        row = _new_connection(obj, payload, now)
        db.add(row)
        rows.append(row)
    if not rows:
        return bulk("connections", [], failed, op="create", partial=False)
    await db.commit()
    for row in rows:
        if row.enabled == 1:
            await _sync_quietly(row, db)
    return bulk("connections", [_serialize(r) for r in rows], failed, op="create", partial=body.partial_allowed)


# --- edit ----------------------------------------------------------------------


@router.patch("/bulk/{ids}", dependencies=[Depends(rate_limit("connections_write", capacity=10, per_minute=10))])
async def patch_connections_bulk(
    ids: str,
    body: BulkBody[ConnectionPatch] = Depends(bulk_body("connections", ConnectionPatch, max_items=MAX_BULK)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Element i of data.connections is applied to the i-th id of the path."""
    id_list = parse_ids(ids, MAX_BULK)
    if len(id_list) != len(body.items):
        raise AppError(
            VALIDATION_ERROR, "Some fields are invalid",
            fields={"connections": field_error(
                "length must match the number of ids in the path",
                type="array", actual_length=len(body.items), value=len(id_list))},
        )
    failed: list[dict] = []
    done: list[tuple[Connection, bool, dict, dict]] = []
    for item, cid in zip(body.items, id_list):
        if item.error is not None:
            failed.append({"id": cid, "error": item.error})
            continue
        try:
            conn = await _get_visible_connection(cid, payload, db, for_update=True)
            fields = item.obj.model_dump(exclude_unset=True)
            changed, resync, old_meta = _apply_patch(conn, fields, payload)
            if changed and conn.provider == "ufanet-smart-home":
                await _delete_connection_devices(conn, db)
                resync = True
        except AppError as exc:
            if not body.partial_allowed:
                await db.rollback()
                raise
            failed.append(failed_item(cid, _err(exc), exc.code))
            continue
        done.append((conn, resync, old_meta, fields))

    if not done:
        await db.rollback()
        return bulk("connections", [], failed, op="edit", partial=False)
    await db.commit()
    for conn, resync, old_meta, fields in done:
        await _evict_pool(old_meta, fields.get("meta"))
        if resync and conn.enabled == 1:
            await _sync_quietly(conn, db)
    return bulk("connections", [_serialize(c) for c, *_ in done], failed, op="edit", partial=body.partial_allowed)


@router.patch(
    "/{connection_id}",
    dependencies=[Depends(rate_limit("connections_write", capacity=10, per_minute=10))],
)
async def patch_connection(
    connection_id: int,
    body: SingleBody[ConnectionPatch] = Depends(_single_body("connection", ConnectionPatch)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    conn = await _get_visible_connection(connection_id, payload, db, for_update=True)
    fields = body.obj.model_dump(exclude_unset=True)
    changed, should_resync, old_meta = _apply_patch(conn, fields, payload)
    if changed and conn.provider == "ufanet-smart-home":
        await _delete_connection_devices(conn, db)
        should_resync = True
    await db.commit()

    await _evict_pool(old_meta, fields.get("meta"))

    if should_resync and conn.enabled == 1:
        await _sync_quietly(conn, db)

    return edited("connection", _serialize(conn), changed=changed)


# --- delete --------------------------------------------------------------------


@router.delete(
    "/bulk/{ids}",
    dependencies=[Depends(rate_limit("connections_write", capacity=10, per_minute=10))],
)
async def delete_connections_bulk(
    ids: str,
    options=Depends(_partial_option),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    id_list = parse_ids(ids, MAX_BULK)
    failed: list[dict] = []
    succeeded: list[dict] = []
    keys = []
    for cid in id_list:
        try:
            conn = await _get_visible_connection(cid, payload, db, for_update=True)
            keys.append(await _delete_conn_row(conn, payload, db))
        except AppError as exc:
            if not options.partial_allowed:
                await db.rollback()
                raise
            failed.append(failed_item(cid, _err(exc), exc.code))
            continue
        succeeded.append({"id": cid})

    if not succeeded:
        await db.rollback()
        return bulk("connections", [], failed, op="delete", partial=False)
    await db.commit()
    for key in keys:
        await _evict_deleted(key)
    return bulk("connections", succeeded, failed, op="delete", partial=options.partial_allowed)


@router.delete(
    "/{connection_id}",
    dependencies=[Depends(rate_limit("connections_write", capacity=10, per_minute=10))],
)
async def delete_connection(
    connection_id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    conn = await _get_visible_connection(connection_id, payload, db, for_update=True)
    key = await _delete_conn_row(conn, payload, db)
    await db.commit()
    await _evict_deleted(key)
    return deleted("connection", {"id": connection_id})


# --- sync (custom endpoint) ----------------------------------------------------


@router.post(
    "/{connection_id}/sync",
    dependencies=[Depends(rate_limit("connections_write", capacity=10, per_minute=10))],
)
async def sync_connection_now(
    connection_id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Manually run the sync for this connection. Useful right after the user
    adds a device to their Tuya app — they can hit this instead of waiting
    ~7 minutes for the scheduled tick. No request body."""
    conn = await _get_visible_connection(connection_id, payload, db)
    if not _can_manage(conn, payload):
        raise AppError(FORBIDDEN_RESOURCE, "Синхронизация доступна только владельцу или admin")

    if conn.enabled != 1:
        raise AppError(CONNECTION_INVALID, "Соединение отключено (enabled=0)")

    await connection_sync.sync_connection_now(conn.id)
    await db.refresh(conn)
    return single("connection", _serialize(conn))
