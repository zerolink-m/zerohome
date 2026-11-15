import time

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import BAD_REQUEST, EMAIL_TAKEN, FORBIDDEN_RESOURCE, OBJECT_NOT_FOUND
from app.core.errors import AppError
from app.core.introspection import supports
from app.core.response import (
    add_warning, bulk, bulk_listing, created, deleted, edited, failed_item, listing, single,
)
from app.core.validation import field_error
from app.deps.ids import parse_ids, partial_query
from app.core.rate_limit import rate_limit
from app.core.security import hash_password, TokenPayload
from app.db.session import get_db
from app.deps.auth import require_access, require_access_admin
from app.deps.list_query import ListQuery, list_query
from app.deps.request_body import BulkBody, SingleBody, bulk_body, single_body
from app.models.cams import Cam
from app.models.connection import Connection
from app.models.invitation import Invitation
from app.models.session import Session as SessionModel
from app.models.sharing import Sharing
from app.models.switch import Switch
from app.models.user import User
from app.schemas.user import UserCreate, UserOut, UserPatch
from app.services import stream_manager

router = APIRouter(prefix="/v1/users", tags=["users"])

USERS_COLUMNS = ["id", "name", "surname", "email", "role", "added_at", "updated_at"]
users_query = list_query(model=User, sortable=USERS_COLUMNS, searchable=USERS_COLUMNS)
MAX_BULK = 100
ADMIN_ONLY_FIELDS = ("role", "allow_record", "allow_tuya_local", "motion_detection_allowed")


def _to_out(user: User) -> dict:
    return UserOut(
        id=user.id,
        name=user.name,
        surname=user.surname,
        email=user.email,
        role=user.role,
        allow_record=user.allowrecord or 0,
        allow_tuya_local=user.allowtuyalocal or 0,
        motion_detection_allowed=user.motion_detection_allowed,
        push=user.push,
        push_metadata=user.pushmeta,
        added_at=user.added_at,
        updated_at=user.updated_at,
    ).model_dump()



def _check_user_access(payload: TokenPayload, id: int) -> None:
    if payload.role != "admin" and payload.uid != id:
        raise AppError(FORBIDDEN_RESOURCE, "Нет доступа к этому пользователю")


async def _get_user(db: AsyncSession, id: int) -> User:
    user = await db.get(User, id)
    if user is None:
        raise AppError(OBJECT_NOT_FOUND, "Пользователь не найден", path={
            "id": field_error("object not found", type="int", value=id)})
    return user


async def _new_user(db: AsyncSession, name, surname, email, password, role) -> User:
    now = int(time.time())
    user = User(
        name=name, surname=surname, email=email,
        password=await hash_password(password), role=role or "user",
        added_at=now, updated_at=now,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise AppError(EMAIL_TAKEN, "Email уже занят", fields={
            "email": field_error("already taken", type="string", value=email)})
    return user


async def _apply_patch(db: AsyncSession, user: User, body: UserPatch, payload: TokenPayload) -> bool:
    """Apply a patch (no commit). Returns True if anything changed."""
    before = _to_out(user)
    fields = body.model_dump(exclude_unset=True, exclude={"password"})

    if payload.role != "admin":
        dropped = [f for f in ADMIN_ONLY_FIELDS if f in fields]
        for f in dropped:
            fields.pop(f)
        if dropped:
            add_warning("Fields ignored (admin only): " + ", ".join(dropped))

    if "name" in fields:
        user.name = fields["name"]
    if "surname" in fields:
        user.surname = fields["surname"]
    if "email" in fields:
        user.email = fields["email"]
    if "role" in fields and fields["role"] is not None:
        user.role = fields["role"]
    if "allow_record" in fields and fields["allow_record"] is not None:
        user.allowrecord = fields["allow_record"]
    if "allow_tuya_local" in fields and fields["allow_tuya_local"] is not None:
        user.allowtuyalocal = fields["allow_tuya_local"]
    if "motion_detection_allowed" in fields and fields["motion_detection_allowed"] is not None:
        user.motion_detection_allowed = fields["motion_detection_allowed"]
    if "push" in fields:
        user.push = fields["push"]
        user.pushmeta = fields.get("push_metadata")
    changed = _to_out(user) != before
    if body.password is not None:
        user.password = await hash_password(body.password)
        changed = True
    if changed:
        user.updated_at = int(time.time())
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise AppError(EMAIL_TAKEN, "Email уже занят", fields={
            "email": field_error("already taken", type="string", value=fields.get("email"))})
    return changed


async def _delete_user_cascade(db: AsyncSession, id: int, user: User) -> None:
    owned_cams = (await db.execute(select(Cam).where(Cam.ownuser == id))).scalars().all()
    for cam in owned_cams:
        await stream_manager.stop(cam.id)
        await db.execute(
            Invitation.__table__.delete().where(
                Invitation.device_type == "cams", Invitation.device_id == cam.id
            )
        )
        await db.execute(
            Sharing.__table__.delete().where(Sharing.device_type == "cams", Sharing.device_id == cam.id)
        )
        await db.delete(cam)

    owned_switches = (await db.execute(select(Switch).where(Switch.ownuser == id))).scalars().all()
    for switch in owned_switches:
        await db.execute(
            Invitation.__table__.delete().where(
                Invitation.device_type == "switches", Invitation.device_id == switch.id
            )
        )
        await db.execute(
            Sharing.__table__.delete().where(
                Sharing.device_type == "switches", Sharing.device_id == switch.id
            )
        )
        await db.delete(switch)

    owned_connections = (
        await db.execute(select(Connection).where(Connection.uid == id))
    ).scalars().all()
    for connection in owned_connections:
        await db.delete(connection)

    sessions = (await db.execute(select(SessionModel).where(SessionModel.uid == id))).scalars().all()
    for session in sessions:
        await db.delete(session)

    await db.delete(user)
    await db.flush()


# --- CRUD ----------------------------------------------------------------------

@router.get("", dependencies=[Depends(rate_limit("users_read", capacity=60, per_minute=60))])
async def list_users(
    q: ListQuery = Depends(users_query),
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User)
    if payload.role != "admin":
        stmt = stmt.where(User.id == payload.uid)
    rows, total = await q.paginate(db, stmt)
    return listing("users", [_to_out(u) for u in rows], query=q, total=total)


@router.post("", dependencies=[Depends(rate_limit("users_write", capacity=20, per_minute=20))])
async def create_user(
    payload: TokenPayload = Depends(require_access_admin),
    req: SingleBody[UserCreate] = Depends(single_body("user", UserCreate)),
    db: AsyncSession = Depends(get_db),
):
    b = req.obj
    user = await _new_user(db, b.name, b.surname, b.email, b.password, b.role)
    out = _to_out(user)
    await db.commit()
    return created("user", out)


@router.post("/bulk", dependencies=[Depends(rate_limit("users_write", capacity=20, per_minute=20))])
async def create_users_bulk(
    payload: TokenPayload = Depends(require_access_admin),
    req: BulkBody[UserCreate] = Depends(bulk_body("users", UserCreate, max_items=MAX_BULK)),
    db: AsyncSession = Depends(get_db),
):
    partial = req.partial_allowed
    succeeded: list[dict] = []
    failed: list[dict] = req.invalid()
    for index, b in req.valid():
        try:
            user = await _new_user(db, b.name, b.surname, b.email, b.password, b.role)
            succeeded.append(_to_out(user))
            if partial:
                await db.commit()
        except AppError as e:
            if not partial:
                raise
            failed.append(failed_item(index, e.error_object()))
    if not partial or succeeded:
        await db.commit()
    failed.sort(key=lambda f: f["id"])
    return bulk("users", succeeded, failed, op="create", partial=partial)


@router.get("/bulk/{ids}", dependencies=[Depends(rate_limit("users_read", capacity=60, per_minute=60))])
@supports("bulk", max_bulk=MAX_BULK)
async def get_users_bulk(
    ids: str,
    payload: TokenPayload = Depends(require_access),
    db: AsyncSession = Depends(get_db),
):
    id_list = parse_ids(ids, MAX_BULK)
    for i in id_list:
        _check_user_access(payload, i)
    rows = (await db.execute(select(User).where(User.id.in_(id_list)))).scalars().all()
    missing = sorted(set(id_list) - {u.id for u in rows})
    if missing:
        raise AppError(OBJECT_NOT_FOUND, "Пользователи не найдены", path={
            "ids": field_error("objects not found", value=",".join(map(str, missing)))})
    by_id = {u.id: u for u in rows}
    return bulk_listing("users", [_to_out(by_id[i]) for i in id_list], requested=len(id_list))


@router.patch("/bulk/{ids}", dependencies=[Depends(rate_limit("users_write", capacity=20, per_minute=20))])
@supports("bulk", "partial", max_bulk=MAX_BULK)
async def patch_users_bulk(
    ids: str,
    payload: TokenPayload = Depends(require_access),
    req: SingleBody[UserPatch] = Depends(single_body("user", UserPatch)),
    db: AsyncSession = Depends(get_db),
):
    id_list = parse_ids(ids, MAX_BULK)
    partial = req.partial_allowed
    succeeded: list[dict] = []
    failed: list[dict] = []
    for i in id_list:
        try:
            _check_user_access(payload, i)
            user = await _get_user(db, i)
            await _apply_patch(db, user, req.obj, payload)
            succeeded.append(_to_out(user))
            if partial:
                await db.commit()
        except AppError as e:
            await db.rollback()
            if not partial:
                raise
            failed.append(failed_item(i, e))
    if not partial or succeeded:
        await db.commit()
    return bulk("users", succeeded, failed, op="edit", partial=partial)


@router.delete("/bulk/{ids}", dependencies=[Depends(rate_limit("users_write", capacity=20, per_minute=20))])
@supports("bulk", "partial", max_bulk=MAX_BULK)
async def delete_users_bulk(
    ids: str,
    partial_allowed: bool = Depends(partial_query),
    payload: TokenPayload = Depends(require_access_admin),
    db: AsyncSession = Depends(get_db),
):
    id_list = parse_ids(ids, MAX_BULK)
    succeeded: list[dict] = []
    failed: list[dict] = []
    for i in id_list:
        try:
            user = await _get_user(db, i)
            await _delete_user_cascade(db, i, user)
            succeeded.append({"id": i})
            if partial_allowed:
                await db.commit()
        except AppError as e:
            await db.rollback()
            if not partial_allowed:
                raise
            failed.append(failed_item(i, e))
    if not partial_allowed or succeeded:
        await db.commit()
    return bulk("users", succeeded, failed, op="delete", partial=partial_allowed)


@router.get("/me", dependencies=[Depends(rate_limit("users_read", capacity=60, per_minute=60))])
async def get_current_user(payload: TokenPayload = Depends(require_access), db: AsyncSession = Depends(get_db)):
    return single("user", _to_out(await _get_user(db, payload.uid)))


@router.get("/{id}", dependencies=[Depends(rate_limit("users_read", capacity=60, per_minute=60))])
async def get_user(id: int, payload: TokenPayload = Depends(require_access), db: AsyncSession = Depends(get_db)):
    _check_user_access(payload, id)
    return single("user", _to_out(await _get_user(db, id)))


@router.patch("/{id}", dependencies=[Depends(rate_limit("users_write", capacity=20, per_minute=20))])
async def patch_user(
    id: int,
    payload: TokenPayload = Depends(require_access),
    req: SingleBody[UserPatch] = Depends(single_body("user", UserPatch)),
    db: AsyncSession = Depends(get_db),
):
    _check_user_access(payload, id)
    user = await _get_user(db, id)
    changed = await _apply_patch(db, user, req.obj, payload)
    out = _to_out(user)
    await db.commit()
    return edited("user", out, changed=changed)


@router.delete("/{id}", dependencies=[Depends(rate_limit("users_write", capacity=20, per_minute=20))])
async def delete_user(id: int, payload: TokenPayload = Depends(require_access_admin), db: AsyncSession = Depends(get_db)):
    user = await _get_user(db, id)
    await _delete_user_cascade(db, id, user)
    await db.commit()
    return deleted("user", {"id": id})
