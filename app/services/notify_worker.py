"""Sends notification_jobs. One loop, started and stopped like the other workers.

CLUSTER_ENABLED=false: this process sends (there is only one). CLUSTER_ENABLED=true: send only
while we hold the global 'notify' lease (resource_id 0). The lease is taken here, not by
ClusterRunner, so the two loops stay separate; the right is the same one, so two nodes never
intentionally send at once. A node whose notify capability is off never takes it.

A job with a photo_key waits while the motion row is still 'uploading' (not a failed attempt).
Once the photo is 'ready' the bytes are read from the storage layer and attached. 'failed', a
missing object, or an upload that never finished, is delivered as text.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time

from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.cams import Cam
from app.models.motion_event import MotionEvent
from app.models.push_settings import PushSettings
from app.models.switch import Switch
from app.models.user import User
from app.services import notify_queue, push, storage as storage_layer
from app.services.cluster import leases
from app.services.cluster.identity import get_node_id, local_capabilities

logger = logging.getLogger(__name__)

_POLL_SECONDS = 1.0
_BATCH_LIMIT = 20
_CLAIM_LEASE_SECONDS = 60
_MAX_ATTEMPTS = 5
_UPLOAD_WAIT_SECONDS = 60
_DEFER_SECONDS = 2
_CLEANUP_EVERY_SECONDS = 3600
_NOTIFY_FUNCTION = "notify"
_NOTIFY_RESOURCE_ID = 0

_FILTERS = {
    "notify_online": lambda ps: ps.notify_online == 1,
    "notify_offline": lambda ps: ps.notify_offline == 1,
    "notify_state_change": lambda ps: ps.notify_state_change == 1,
}

_task: asyncio.Task | None = None
_lease: leases.Lease | None = None
_last_purge = 0
# This process only. Another process with the same NODE_ID must not renew our notify lease.
_HOLDER = secrets.token_hex(16)


def start() -> None:
    global _task, _last_purge
    if _task is None or _task.done():
        _last_purge = int(time.time())
        _task = asyncio.create_task(_loop(), name="notify-worker")
        logger.info("notification worker started")


async def stop() -> None:
    """Stops the loop and releases the notify lease (idempotent; safe if never started)."""
    global _task
    task, _task = _task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("notification worker raised during stop")
    await _release_held()


async def _release_held() -> None:
    global _lease
    lease, _lease = _lease, None
    if lease is None:
        return
    try:
        async with AsyncSessionLocal() as db:
            await leases.release(db, lease)
    except Exception:
        logger.warning("notification worker failed to release the notify lease")


async def _allowed_to_send() -> bool:
    """Notify capability off: do not send. Otherwise one process always sends.
    A cluster node sends only while it holds the 'notify' lease."""
    global _lease
    if not local_capabilities(settings).get("notify"):
        await _release_held()
        return False
    if not settings.CLUSTER_ENABLED:
        return True
    node_id = get_node_id()
    now = int(time.time())
    ttl = int(settings.LEASE_TTL_SECONDS)
    try:
        async with AsyncSessionLocal() as db:
            if _lease is not None and await leases.renew(db, _lease, ttl, now):
                return True
            _lease = None
            got = await leases.try_acquire(
                db, _NOTIFY_FUNCTION, _NOTIFY_RESOURCE_ID, node_id, ttl, now, _HOLDER,
            )
    except Exception:
        logger.warning("notification worker lease failed", exc_info=True)
        return False
    if got is None:
        return False
    _lease = got
    logger.info("notification worker holds notify (generation %s)", got.generation)
    return True


async def _maybe_purge(now: int) -> None:
    global _last_purge
    if now - _last_purge < _CLEANUP_EVERY_SECONDS:
        return
    _last_purge = now
    try:
        async with AsyncSessionLocal() as db:
            removed = await notify_queue.purge_finished(db, now, settings.NOTIFY_JOB_RETENTION_DAYS)
        if removed:
            logger.info("notification worker removed %s old jobs", removed)
    except Exception:
        logger.warning("notification job cleanup failed", exc_info=True)


async def _loop() -> None:
    while True:
        try:
            if await _allowed_to_send():
                lease = _lease if settings.CLUSTER_ENABLED else None
                await deliver_batch(lease=lease)
                await _maybe_purge(int(time.time()))
                delay = _POLL_SECONDS
            else:
                delay = float(max(1, int(settings.LEASE_RENEW_INTERVAL_SECONDS)))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("notification worker iteration failed")
            delay = _POLL_SECONDS
        await asyncio.sleep(delay)


async def _keep_delivery_right(lease: leases.Lease, job) -> bool:
    global _lease
    if _lease is not lease or not local_capabilities(settings).get("notify"):
        return False
    async with AsyncSessionLocal() as db:
        if not await leases.renew(db, lease, settings.LEASE_TTL_SECONDS, int(time.time())):
            _lease = None
            return False
        return await notify_queue.renew_claim(db, job, int(time.time()), _CLAIM_LEASE_SECONDS)


async def _deliver_with_lease(lease: leases.Lease, job, now: int, sent: list[int]) -> bool:
    interval = max(1, min(int(settings.LEASE_RENEW_INTERVAL_SECONDS), _CLAIM_LEASE_SECONDS // 3))

    async def renew_right() -> bool:
        try:
            return await asyncio.wait_for(_keep_delivery_right(lease, job), timeout=interval)
        except Exception:
            logger.warning("Notification delivery lease renewal failed", exc_info=True)
            return False

    async def maintain_right() -> None:
        while True:
            await asyncio.sleep(interval)
            if not await renew_right():
                return

    # Confirm both rights before starting even the first send. Continue to renew
    # while storage, recipient lookups and the provider's HTTP calls are in flight.
    if not await renew_right():
        return False
    delivery = asyncio.create_task(_handle(job, now, sent))
    renewal = asyncio.create_task(maintain_right())
    try:
        done, _ = await asyncio.wait({delivery, renewal}, return_when=asyncio.FIRST_COMPLETED)
        if delivery in done:
            await delivery
            return True
        await renewal
        return False
    finally:
        for task in (delivery, renewal):
            if not task.done():
                task.cancel()
        await asyncio.gather(delivery, renewal, return_exceptions=True)


async def deliver_batch(now: int | None = None, node_id: str | None = None,
                        *, lease: leases.Lease | None = None) -> int:
    """Deliver up to one batch of jobs and return the number handled."""
    clock = time.time if now is None else lambda: now
    node_id = node_id or get_node_id()
    handled = 0
    for _ in range(_BATCH_LIMIT):
        current = int(clock())
        async with AsyncSessionLocal() as db:
            jobs = await notify_queue.claim(db, node_id, current, 1, _CLAIM_LEASE_SECONDS)
        if not jobs:
            break
        job = jobs[0]
        sent: list[int] = []
        try:
            if lease is None:
                await _handle(job, current, sent)
            elif not await _deliver_with_lease(lease, job, current, sent):
                async with AsyncSessionLocal() as db:
                    await notify_queue.defer(db, job, current, _DEFER_SECONDS, delivered=sent)
                break
        except asyncio.CancelledError:
            if lease is not None:
                with contextlib.suppress(Exception):
                    async with AsyncSessionLocal() as db:
                        await notify_queue.defer(db, job, current, _DEFER_SECONDS, delivered=sent)
            raise
        except Exception:
            logger.exception("notification job %s failed", job.id)
            async with AsyncSessionLocal() as db:
                await notify_queue.fail(db, job, int(time.time()), _MAX_ATTEMPTS, delivered=sent)
            if lease is not None:
                break
        handled += 1
    return handled


async def _handle(job, now: int, sent: list[int]) -> None:
    async with AsyncSessionLocal() as db:
        action = await _photo_action(db, job, now)
    if action == "wait":
        async with AsyncSessionLocal() as db:
            await notify_queue.defer(db, job, now, _DEFER_SECONDS)
        return
    photo: bytes | None = None
    if action == "send" and job.photo_key:
        try:
            photo = await storage_layer.get_storage().get(job.photo_key)
        except Exception as exc:
            logger.warning("notification job %s photo read failed: %s", job.id, type(exc).__name__)
            async with AsyncSessionLocal() as db:
                await notify_queue.fail(db, job, int(time.time()), _MAX_ATTEMPTS)
            return
    async with AsyncSessionLocal() as db:
        ok, _ = await _send(db, job, photo, sent)
    async with AsyncSessionLocal() as db:
        if ok:
            await notify_queue.complete(db, job, int(time.time()))
        else:
            await notify_queue.fail(db, job, int(time.time()), _MAX_ATTEMPTS, delivered=sent)


async def _photo_action(db, job, now: int) -> str:
    """'wait' (still uploading), 'text' (send the message only), 'send' (attach the object if present)."""
    if not job.photo_key:
        return "text"
    event = (
        await db.execute(
            select(MotionEvent)
            .where(MotionEvent.photo_key == job.photo_key)
            .order_by(MotionEvent.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if event is None or event.photo_state in (None, "ready"):
        return "send"
    if event.photo_state == "uploading" and now - int(event.detected_at) < _UPLOAD_WAIT_SECONDS:
        return "wait"
    return "text"


async def _send(db, job, photo: bytes | None, sent: list[int]) -> tuple[bool, list[int]]:
    payload = job.payload if isinstance(job.payload, dict) else {}
    skip = notify_queue.delivered_ids(payload)
    if job.kind == "motion":
        return await _send_motion(db, job, photo, payload, sent, skip)
    filt = _FILTERS.get(payload.get("filter"))
    if payload.get("filter"):
        if filt is None:
            logger.warning("notification job %s has an unknown filter", job.id)
            return True, sent
        ok = await push._notify_recipients_filtered(
            db, job.device_type or "", job.device_id or 0, job.message, filter_func=filt,
            delivered=sent, skip_ids=skip,
        )
        return ok, sent
    return await _send_manual(db, job, sent, skip)


async def _send_motion(db, job, photo: bytes | None, payload: dict,
                       sent: list[int], skip: set[int]) -> tuple[bool, list[int]]:
    user_ids = {
        uid for uid in (payload.get("user_ids") or [])
        if isinstance(uid, int) and not isinstance(uid, bool) and uid not in skip
    }
    if job.device_type != "cams" or not job.device_id or not user_ids:
        return True, sent
    event = None
    if job.photo_key:
        event = (await db.scalars(select(MotionEvent).where(
            MotionEvent.cam_id == job.device_id, MotionEvent.photo_key == job.photo_key,
        ).order_by(MotionEvent.id.desc()).limit(1))).first()
    object_type = event.object_type if event is not None else payload.get("object_type")
    recipients = (
        push.users_with_device_access("cams", job.device_id)
        .join(PushSettings, PushSettings.user_id == User.id)
        .where(
            User.id.in_(user_ids), PushSettings.device_type == "cams",
            PushSettings.device_id == job.device_id, PushSettings.enabled == 1,
            PushSettings.motion_detection_enabled == 1,
        )
    )
    if object_type in ("person", "dog", "cat", "car"):
        recipients = recipients.where(getattr(PushSettings, f"motion_detect_{object_type}") == 1)
    if event is not None:
        recipients = recipients.where(
            PushSettings.motion_min_confidence <= event.confidence,
            PushSettings.motion_min_object_size <= event.object_size,
        )
    name = job.photo_key.rsplit("/", 1)[-1] if job.photo_key else "photo.jpg"
    ok = True
    for uid in sorted(user_ids):
        # Open a fresh transaction per recipient so a revoke during an earlier
        # send is visible even at MySQL's default REPEATABLE READ isolation.
        async with AsyncSessionLocal() as read_db:
            user = (await read_db.scalars(recipients.where(User.id == uid))).first()
        if user is None:
            continue
        if await push.notify_user(user, job.message, photo_bytes=photo, photo_name=name):
            sent.append(user.id)
        else:
            ok = False
    return ok, sent


async def _send_manual(db, job, sent: list[int], skip: set[int]) -> tuple[bool, list[int]]:
    if job.device_type == "cams":
        cam = await db.get(Cam, job.device_id)
        if cam is None:
            return True, sent
        ok = await push.notify_cam_recipients(db, cam, job.message, delivered=sent, skip_ids=skip)
        return ok, sent
    if job.device_type == "switches":
        switch = await db.get(Switch, job.device_id)
        if switch is None:
            return True, sent
        ok = await push.notify_switch_recipients(db, switch, job.message, delivered=sent, skip_ids=skip)
        return ok, sent
    logger.warning("notification job %s has no device", job.id)
    return True, sent
