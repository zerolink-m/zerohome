import time

from fastapi import APIRouter, Depends
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Request

from app.core.codes import (
    BULK_LIMIT_EXCEEDED,
    FORBIDDEN_RESOURCE,
    OBJECT_NOT_FOUND,
    SERVER_ERROR,
    TUYA_LOCAL_NOT_ALLOWED,
    VALIDATION_ERROR,
)
from app.core.errors import AppError
from app.core.introspection import declare
from app.core.response import bulk, bulk_listing, created, deleted, edited, failed_item, listing, single
from app.deps.ids import parse_ids
from app.core.validation import field_error
from app.core.rate_limit import rate_limit
from app.core.security import TokenPayload
from app.db.session import get_db
from app.deps.auth import AccessContext, require_access, resolve_device_use_access
from app.deps.list_query import ListQuery, list_query
from app.deps.request_body import BulkBody, SingleBody, bulk_body, read_envelope, single_body
from app.models.invitation import Invitation
from app.models.sharing import Sharing
from app.models.switch import Switch
from app.models.user import User
from app.schemas.switch import (
    SwitchCreate,
    SwitchOutFull,
    SwitchOutRestricted,
    SwitchPatch,
    SwitchStateIn,
    SwitchStateOut,
)
from app.services import push, switch_client

router = APIRouter(prefix="/v1/switches", tags=["switches"])

SWITCHES_COLUMNS = {
    "id": Switch.id,
    "name": Switch.name,
    "enabled": Switch.enabled,
    "control_protocol": Switch.control_proto,
    "state": Switch.state,
    "added_at": Switch.added_at,
    "updated_at": Switch.updated_at,
}
switches_query = list_query(model=Switch, sortable=SWITCHES_COLUMNS, searchable=SWITCHES_COLUMNS)
MAX_BULK = 100


def _invitation_exists_clause(payload: TokenPayload):
    return exists(
        select(Invitation.id)
        .join(User, func.lower(User.email) == func.lower(Invitation.email))
        .where(
            Invitation.device_type == "switches",
            Invitation.device_id == Switch.id,
            Invitation.status == "accepted",
            User.id == payload.uid,
        )
    )


def _visibility_where(payload: TokenPayload):
    if payload.role == "admin":
        return None
    return or_(
        Switch.ownuser == payload.uid,
        and_(Switch.enabled == 1, _invitation_exists_clause(payload)),
    )


def _can_manage(switch: Switch, payload: TokenPayload) -> bool:
    return payload.role == "admin" or switch.ownuser == payload.uid


def _serialize(switch: Switch, payload: TokenPayload) -> dict:
    if payload.role == "admin" or switch.ownuser == payload.uid:
        return SwitchOutFull(
            id=switch.id,
            owner=switch.ownuser,
            name=switch.name,
            enabled=switch.enabled,
            notify=switch.notify,
            control_protocol=switch.control_proto,
            control_metadata=switch.controlmeta,
            state=switch.state,
            state_updated_at=switch.state_updated_at,
            updated_at=switch.updated_at,
            added_at=switch.added_at,
        ).model_dump()
    return SwitchOutRestricted(
        id=switch.id,
        owner=switch.ownuser,
        name=switch.name,
        enabled=switch.enabled,
        control_protocol=switch.control_proto,
        state=switch.state,
        updated_at=switch.updated_at,
        added_at=switch.added_at,
    ).model_dump()


async def _get_visible_switch(id: int, payload: TokenPayload, db: AsyncSession) -> Switch:
    stmt = select(Switch).where(Switch.id == id)
    condition = _visibility_where(payload)
    if condition is not None:
        stmt = stmt.where(condition)
    switch = (await db.execute(stmt)).scalar_one_or_none()
    if switch is None:
        raise AppError(OBJECT_NOT_FOUND, "Устройство не найдено")
    return switch


async def _get_switch_for_access(id: int, ctx: AccessContext, db: AsyncSession) -> Switch:
    if ctx.payload is not None:
        return await _get_visible_switch(id, ctx.payload, db)
    switch = await db.get(Switch, id)
    if switch is None:
        raise AppError(OBJECT_NOT_FOUND, "Устройство не найдено")
    return switch


async def _check_tuya_local_allowed(control_protocol: str, payload: TokenPayload, db: AsyncSession) -> None:
    if control_protocol != "tuya_local":
        return
    user = await db.get(User, payload.uid)
    if user is None or user.allowtuyalocal != 1:
        raise AppError(TUYA_LOCAL_NOT_ALLOWED, "У пользователя не включён флаг allow_tuya_local")



async def _partial_option(request: Request):
    """Optional body for bulk DELETE: only options.partial_allowed / ?partial_allowed matter."""
    _, options = await read_envelope(request, optional=True)
    return options


declare(_partial_option, "bulk", "partial", max_bulk=MAX_BULK)


async def _with_children(sw_dict: dict, switch: Switch, payload: TokenPayload, db: AsyncSession) -> dict:
    if switch.is_master == 1:
        condition = _visibility_where(payload)
        children_stmt = select(Switch).where(Switch.parent == switch.id)
        if condition is not None:
            children_stmt = children_stmt.where(condition)
        children_rows = (await db.execute(children_stmt)).scalars().all()
        sw_dict["children"] = [_serialize(ch, payload) for ch in children_rows]
    return sw_dict


def _new_switch(body: SwitchCreate, payload: TokenPayload, now: int) -> Switch:
    return Switch(
        ownuser=payload.uid,
        name=body.name if body.name is not None else "",
        enabled=body.enabled if body.enabled is not None else 0,
        notify=body.notify if body.notify is not None else 0,
        control_proto=body.control_protocol,
        controlmeta=body.control_metadata,
        state=0,
        state_updated_at=now,
        added_at=now,
        updated_at=now,
    )


async def _apply_patch(switch: Switch, fields: dict, payload: TokenPayload, db: AsyncSession) -> tuple[bool, bool]:
    """Validate permissions and mutate `switch` (no commit). Returns (changed, notify_enabled)."""
    if not _can_manage(switch, payload):
        raise AppError(FORBIDDEN_RESOURCE, "Изменение устройства доступно только владельцу или admin")

    # Auto-synced switches are managed by the sync worker: the user may only
    # rename them, toggle enabled, or toggle notify. control_protocol/control_metadata
    # would be stomped on the next tick anyway, so refusing early gives a
    # clearer error than "your edit silently vanished".
    if switch.added_by == "auto":
        forbidden_on_auto = {"control_protocol", "control_metadata"} & set(fields.keys())
        if forbidden_on_auto:
            raise AppError(
                FORBIDDEN_RESOURCE,
                f"Для auto-устройств запрещено менять поля: {sorted(forbidden_on_auto)}",
            )

    target_controlproto = fields.get("control_protocol", switch.control_proto)
    if target_controlproto != switch.control_proto or "control_metadata" in fields:
        await _check_tuya_local_allowed(target_controlproto, payload, db)

    notify_enabled = fields.get("notify") == 1 and switch.notify != 1

    changed = False
    for key, value in fields.items():
        attr = {"control_protocol": "control_proto", "control_metadata": "controlmeta"}.get(key, key)
        if getattr(switch, attr) != value:
            changed = True
        setattr(switch, attr, value)
    if changed:
        switch.updated_at = int(time.time())
    return changed, notify_enabled


async def _delete_switch_row(switch: Switch, payload: TokenPayload, db: AsyncSession) -> None:
    """Permission checks + deletion (no commit)."""
    if not _can_manage(switch, payload):
        raise AppError(FORBIDDEN_RESOURCE, "Удаление устройства доступно только владельцу или admin")

    # Auto-synced switches are owned by their connection — deleting them
    # standalone would just make the next sync tick recreate them. Refuse
    # here and point the user at the underlying connection.
    if switch.added_by == "auto":
        raise AppError(
            FORBIDDEN_RESOURCE,
            "Устройство синхронизировано автоматически. "
            "Удалите его в приложении провайдера или удалите соединение целиком.",
        )

    await db.execute(
        Invitation.__table__.delete().where(
            Invitation.device_type == "switches", Invitation.device_id == switch.id
        )
    )
    await db.execute(
        Sharing.__table__.delete().where(Sharing.device_type == "switches", Sharing.device_id == switch.id)
    )
    await db.delete(switch)


def _err(exc: AppError) -> dict:
    return {"error": exc.message}


# --- list / get ----------------------------------------------------------------


@router.get("", dependencies=[Depends(rate_limit("switches_read", capacity=60, per_minute=60))])
async def list_switches(
    q: ListQuery = Depends(switches_query),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Switch).where(Switch.parent.is_(None))
    condition = _visibility_where(payload)
    if condition is not None:
        stmt = stmt.where(condition)

    rows, total = await q.paginate(db, stmt)

    master_ids = [r.id for r in rows if r.is_master == 1]
    children_by_parent = {}
    if master_ids:
        children_stmt = select(Switch).where(Switch.parent.in_(master_ids))
        if condition is not None:
            children_stmt = children_stmt.where(condition)
        children_rows = (await db.execute(children_stmt)).scalars().all()
        for child in children_rows:
            children_by_parent.setdefault(child.parent, []).append(child)

    items = []
    for switch in rows:
        sw_dict = _serialize(switch, payload)
        if switch.is_master == 1:
            sw_children = children_by_parent.get(switch.id, [])
            sw_dict["children"] = [_serialize(ch, payload) for ch in sw_children]
        items.append(sw_dict)

    return listing("switches", items, query=q, total=total)


@router.get(
    "/bulk/{ids}",
    dependencies=[Depends(rate_limit("switches_read", capacity=60, per_minute=60))],
)
async def get_switches_bulk(
    ids: str,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    id_list = parse_ids(ids, MAX_BULK)
    items = []
    for sid in id_list:
        switch = await _get_visible_switch(sid, payload, db)
        items.append(await _with_children(_serialize(switch, payload), switch, payload, db))
    return bulk_listing("switches", items, requested=len(id_list))


declare(get_switches_bulk, "bulk")


@router.get("/{switch_id}", dependencies=[Depends(rate_limit("switches_read", capacity=60, per_minute=60))])
async def get_switch(switch_id: int, payload: TokenPayload = Depends(require_access), db: AsyncSession = Depends(get_db)):
    switch = await _get_visible_switch(switch_id, payload, db)
    return single("switch", await _with_children(_serialize(switch, payload), switch, payload, db))


# --- create --------------------------------------------------------------------


@router.post("", dependencies=[Depends(rate_limit("switches_write", capacity=20, per_minute=20))])
async def create_switch(
    body: SingleBody[SwitchCreate] = Depends(single_body("switch", SwitchCreate)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    await _check_tuya_local_allowed(body.obj.control_protocol, payload, db)

    switch = _new_switch(body.obj, payload, int(time.time()))
    db.add(switch)
    await db.commit()

    return created("switch", _serialize(switch, payload))


@router.post("/bulk", dependencies=[Depends(rate_limit("switches_write", capacity=20, per_minute=20))])
async def create_switches_bulk(
    body: BulkBody[SwitchCreate] = Depends(bulk_body("switches", SwitchCreate, max_items=MAX_BULK)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    now = int(time.time())
    failed = body.invalid()
    new_rows: list[Switch] = []
    for index, obj in body.valid():
        try:
            await _check_tuya_local_allowed(obj.control_protocol, payload, db)
        except AppError as exc:
            if not body.partial_allowed:
                raise
            failed.append(failed_item(index, _err(exc)))
            continue
        row = _new_switch(obj, payload, now)
        db.add(row)
        new_rows.append(row)
    failed.sort(key=lambda f: f["id"])

    if failed and (not body.partial_allowed or not new_rows):
        await db.rollback()
        return bulk("switches", [], failed, op="create", partial=False)
    await db.commit()
    return bulk("switches", [_serialize(r, payload) for r in new_rows], failed, op="create", partial=body.partial_allowed)


# --- edit ----------------------------------------------------------------------


@router.patch("/bulk/{ids}", dependencies=[Depends(rate_limit("switches_write", capacity=20, per_minute=20))])
async def patch_switches_bulk(
    ids: str,
    body: BulkBody[SwitchPatch] = Depends(bulk_body("switches", SwitchPatch, max_items=MAX_BULK)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    """Element i of data.switches is applied to the i-th id of the path."""
    id_list = parse_ids(ids, MAX_BULK)
    if len(id_list) != len(body.items):
        raise AppError(
            VALIDATION_ERROR, "Some fields are invalid",
            fields={"switches": field_error(
                "length must match the number of ids in the path",
                type="array", actual_length=len(body.items), value=len(id_list))},
        )
    failed: list[dict] = []
    succeeded: list[dict] = []
    pushes: list[Switch] = []
    for item, sid in zip(body.items, id_list):
        if item.error is not None:
            failed.append({"id": sid, "error": item.error})
            continue
        try:
            switch = await _get_visible_switch(sid, payload, db)
            _, notify_on = await _apply_patch(switch, item.obj.model_dump(exclude_unset=True), payload, db)
        except AppError as exc:
            if not body.partial_allowed:
                await db.rollback()
                raise
            failed.append(failed_item(sid, _err(exc), exc.code))
            continue
        succeeded.append(switch)
        if notify_on:
            pushes.append(switch)

    if failed and not succeeded:
        await db.rollback()
        return bulk("switches", [], failed, op="edit", partial=False)
    await db.commit()
    for sw in pushes:
        push.notify_switch_background(sw.id, f"Push включён для устройства {sw.name or sw.id}")
    return bulk("switches", [_serialize(s, payload) for s in succeeded], failed, op="edit", partial=body.partial_allowed)


@router.patch("/{switch_id}", dependencies=[Depends(rate_limit("switches_write", capacity=20, per_minute=20))])
async def patch_switch(
    switch_id: int,
    body: SingleBody[SwitchPatch] = Depends(single_body("switch", SwitchPatch)),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    switch = await _get_visible_switch(switch_id, payload, db)
    changed, notify_enabled = await _apply_patch(switch, body.obj.model_dump(exclude_unset=True), payload, db)
    await db.commit()

    if notify_enabled:
        push.notify_switch_background(switch.id, f"Push включён для устройства {switch.name or switch.id}")

    return edited("switch", _serialize(switch, payload), changed=changed)


# --- delete --------------------------------------------------------------------


@router.delete("/bulk/{ids}", dependencies=[Depends(rate_limit("switches_write", capacity=20, per_minute=20))])
async def delete_switches_bulk(
    ids: str,
    options=Depends(_partial_option),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    id_list = parse_ids(ids, MAX_BULK)
    failed: list[dict] = []
    succeeded: list[dict] = []
    for sid in id_list:
        try:
            switch = await _get_visible_switch(sid, payload, db)
            await _delete_switch_row(switch, payload, db)
        except AppError as exc:
            if not options.partial_allowed:
                await db.rollback()
                raise
            failed.append(failed_item(sid, _err(exc), exc.code))
            continue
        succeeded.append({"id": sid})

    if failed and not succeeded:
        await db.rollback()
        return bulk("switches", [], failed, op="delete", partial=False)
    await db.commit()
    return bulk("switches", succeeded, failed, op="delete", partial=options.partial_allowed)


@router.delete("/{switch_id}", dependencies=[Depends(rate_limit("switches_write", capacity=20, per_minute=20))])
async def delete_switch(
    switch_id: int,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    switch = await _get_visible_switch(switch_id, payload, db)
    await _delete_switch_row(switch, payload, db)
    await db.commit()

    return deleted("switch", {"id": switch_id})


# --- state (custom endpoints) --------------------------------------------------


@router.get(
    "/{id}/state",
    dependencies=[Depends(rate_limit("switches_state_read", capacity=300, per_minute=300))],
)
async def get_switch_state(
    id: int,  # name fixed by resolve_device_use_access (path param `id`)
    ctx: AccessContext = Depends(resolve_device_use_access),
    db: AsyncSession = Depends(get_db),
):
    switch = await _get_switch_for_access(id, ctx, db)
    return single("state", SwitchStateOut(state=switch.state, state_updated_at=switch.state_updated_at).model_dump())


@router.post(
    "/{id}/state",
    dependencies=[Depends(rate_limit("switches_state_write", capacity=30, per_minute=30))],
)
async def set_switch_state(
    id: int,  # name fixed by resolve_device_use_access (path param `id`)
    body: SingleBody[SwitchStateIn] = Depends(single_body("state", SwitchStateIn)),
    ctx: AccessContext = Depends(resolve_device_use_access),
    db: AsyncSession = Depends(get_db),
):
    switch = await _get_switch_for_access(id, ctx, db)
    new_state = body.obj.state

    try:
        await switch_client.set_state(switch, new_state)
    except AppError:
        raise
    except Exception as exc:
        raise AppError(SERVER_ERROR, str(exc)) from exc

    now = int(time.time())
    switch.state = new_state
    switch.state_updated_at = now

    # Master row: the switch_client already fired a batch command touching
    # every channel of the physical device, so all children just flipped
    # to the same state upstream. Mirror that locally in the same tx so
    # the DB doesn't lie until the next sync tick. External changes are
    # still reconciled by the background sync worker.
    if switch.is_master == 1:
        children = (
            await db.execute(select(Switch).where(Switch.parent == switch.id))
        ).scalars().all()
        for child in children:
            if child.state != new_state:
                child.state = new_state
                child.state_updated_at = now

    # Child row: the parent's aggregate state is OR over its siblings.
    # Cheapest correct update — recompute from the children rather than
    # try to derive it from just this row's transition.
    elif switch.parent is not None:
        siblings = (
            await db.execute(
                select(Switch).where(Switch.parent == switch.parent)
            )
        ).scalars().all()
        agg = 1 if any(s.state == 1 for s in siblings) else 0
        master = await db.get(Switch, switch.parent)
        if master is not None and master.state != agg:
            master.state = agg
            master.state_updated_at = now

    await db.commit()

    return single("state", SwitchStateOut(state=switch.state, state_updated_at=switch.state_updated_at).model_dump())
