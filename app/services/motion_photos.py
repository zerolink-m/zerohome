"""Persist a motion event, its photo, and the notification job.

When there are push recipients, the event (photo_state='uploading') and job are
committed together. The bytes are stored afterwards with one retry. If uploading
fails, the worker delivers the notification as text.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import update

from app.db.session import AsyncSessionLocal
from app.models.motion_event import MotionEvent
from app.services import notify_queue
from app.services import storage as storage_layer

logger = logging.getLogger(__name__)


def motion_photo_key(cam_id: int, filename: str) -> str:
    return f"motion/{int(cam_id)}/{filename}"


async def save_motion_event_and_photo(
    *,
    cam_id: int,
    detected_at: int,
    object_type: str,
    confidence: float,
    object_size: float,
    license_plate: str | None,
    photo_filename: str,
    bbox: tuple[int, int, int, int],
    notified_user_ids: list[int],
    message: str,
    jpeg: bytes,
) -> int:
    key = motion_photo_key(cam_id, photo_filename)
    x, y, w, h = bbox
    async with AsyncSessionLocal() as db:
        event = MotionEvent(
            cam_id=cam_id,
            detected_at=detected_at,
            object_type=object_type,
            confidence=confidence,
            object_size=object_size,
            license_plate=license_plate,
            photo_filename=photo_filename,
            photo_key=key,
            photo_state="uploading",
            bbox_x=x,
            bbox_y=y,
            bbox_width=w,
            bbox_height=h,
            notified_user_ids=json.dumps(list(notified_user_ids)),
        )
        db.add(event)
        if notified_user_ids:
            await notify_queue.enqueue(
                db,
                kind="motion",
                device_type="cams",
                device_id=cam_id,
                message=message,
                photo_key=key,
                payload={"object_type": object_type, "user_ids": list(notified_user_ids)},
                now=detected_at,
            )
        await db.commit()
        await db.refresh(event)
        event_id = event.id

    uploaded = await _put_with_one_retry(key, jpeg)
    state = "ready" if uploaded else "failed"
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(MotionEvent).where(MotionEvent.id == event_id).values(photo_state=state)
        )
        await db.commit()
    return event_id


async def _put_with_one_retry(key: str, data: bytes) -> bool:
    storage = storage_layer.get_storage()
    for attempt in (1, 2):
        try:
            await storage.put(key, data, "image/jpeg")
            return True
        except Exception as exc:
            logger.warning(
                "motion photo upload failed (attempt %s): %s", attempt, type(exc).__name__,
            )
    return False
