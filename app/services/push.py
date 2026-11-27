from __future__ import annotations

import asyncio
import logging
import os

import httpx
from sqlalchemy import func, or_, select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.cams import Cam
from app.models.invitation import Invitation
from app.models.push_settings import PushSettings
from app.models.switch import Switch
from app.models.user import User

logger = logging.getLogger(__name__)

# httpx logs every request URL at INFO, and the Telegram URL contains the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

_SEND_TIMEOUT_SECONDS = 5.0
_PHOTO_TIMEOUT_SECONDS = 20.0
_CAPTION_LIMIT = 1024


def _mask(text, bot_token) -> str:
    """Strip the bot token from any text that may end up in logs."""
    text = str(text)
    if bot_token:
        text = text.replace(str(bot_token), "***")
    return text


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _send_text(bot_token, telegram_id, message) -> bool:
    url = f"{settings.TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=_SEND_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url,
                json={"chat_id": telegram_id, "text": message},
            )
        if resp.status_code >= 400:
            logger.warning(
                "telegram push failed: status=%s body=%s",
                resp.status_code,
                _mask(resp.text[:200], bot_token),
            )
            return False
        return True
    except Exception as exc:
        logger.warning("telegram push failed: %s", _mask(exc, bot_token))
        return False


async def _send_photo(
    bot_token,
    telegram_id,
    message,
    photo_path: str | None = None,
    photo_bytes: bytes | None = None,
    photo_name: str = "photo.jpg",
) -> bool:
    """Returns True if the photo was delivered; False means caller should fall back to text."""
    try:
        if photo_bytes is None:
            if not photo_path or not os.path.isfile(photo_path):
                logger.warning("telegram sendPhoto skipped: file not found")
                return False
            data = await asyncio.to_thread(_read_file, photo_path)
            name = os.path.basename(photo_path)
        else:
            data = photo_bytes
            name = os.path.basename(photo_name or "photo.jpg") or "photo.jpg"
        url = f"{settings.TELEGRAM_API_BASE}/bot{bot_token}/sendPhoto"
        async with httpx.AsyncClient(timeout=_PHOTO_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url,
                data={"chat_id": str(telegram_id), "caption": message[:_CAPTION_LIMIT]},
                files={"photo": (name, data, "image/jpeg")},
            )
        if resp.status_code >= 400:
            logger.warning(
                "telegram sendPhoto failed: status=%s body=%s",
                resp.status_code,
                _mask(resp.text[:200], bot_token),
            )
            return False
        return True
    except Exception as exc:
        logger.warning("telegram sendPhoto failed: %s", _mask(exc, bot_token))
        return False


async def _send_telegram(
    bot_token,
    telegram_id,
    message,
    photo_path: str | None = None,
    photo_bytes: bytes | None = None,
    photo_name: str = "photo.jpg",
) -> bool:
    try:
        if (photo_bytes is not None or photo_path) and await _send_photo(
            bot_token, telegram_id, message,
            photo_path=photo_path, photo_bytes=photo_bytes, photo_name=photo_name,
        ):
            return True
        return await _send_text(bot_token, telegram_id, message)
    except Exception as exc:
        logger.warning("telegram push failed: %s", _mask(exc, bot_token))
        return False


async def notify_user(
    user,
    message,
    photo_path: str | None = None,
    *,
    photo_bytes: bytes | None = None,
    photo_name: str = "photo.jpg",
) -> bool:
    """Best-effort push delivery — never raises. False means the send itself failed (retryable)."""
    try:
        if user.push != "telegram":
            return True
        meta = user.pushmeta if isinstance(user.pushmeta, dict) else {}
        telegram = meta.get("telegram") if isinstance(meta.get("telegram"), dict) else None
        if not telegram:
            return True
        bot_token = telegram.get("bot_token")
        telegram_id = telegram.get("telegram_id")
        if not bot_token or not isinstance(telegram_id, int):
            return True
        return await _send_telegram(
            bot_token, telegram_id, message, photo_path, photo_bytes, photo_name,
        )
    except Exception as exc:
        logger.warning("push failed: %s", type(exc).__name__)
        return False


def users_with_device_access(device_type: str, device_id: int, *, include_admins: bool = True):
    """Users eligible to receive job content under the device's current permissions."""
    device = {"cams": Cam, "switches": Switch}[device_type]
    invited = select(Invitation.id).where(
        Invitation.device_type == device_type,
        Invitation.device_id == device.id,
        Invitation.status == "accepted",
        func.lower(Invitation.email) == func.lower(User.email),
    ).correlate(device, User).exists()
    access = [device.ownuser == User.id, invited]
    if include_admins:
        access.append(User.role == "admin")
    return select(User).where(
        select(device.id).where(device.id == device_id, or_(*access)).correlate(User).exists()
    )


async def _notify_recipients(db, device_type: str, device_id: int, message: str,
                             delivered: list | None = None, skip_ids: set | None = None,
                             filter_func=None):
    """Send only while the recipient still has access and push enabled."""
    preference = select(PushSettings).where(
        PushSettings.device_type == device_type,
        PushSettings.device_id == device_id,
    )
    prefs = (await db.scalars(preference)).all()
    configured = bool(prefs)
    recipients = users_with_device_access(device_type, device_id, include_admins=configured)
    if configured or filter_func is not None:
        candidates = [
            ps.user_id for ps in prefs
            if ps.enabled == 1 and (filter_func is None or filter_func(ps))
        ]
        recipients = recipients.where(User.id.in_(candidates))
    ids = (await db.scalars(recipients.with_only_columns(User.id))).all()
    await db.rollback()  # The delivery session is read-only. End its current snapshot.

    ok = True
    for uid in ids:
        if skip_ids and uid in skip_ids:
            continue
        user = (await db.scalars(recipients.where(User.id == uid))).first()
        should_send = user is not None
        if should_send:
            current = (await db.scalars(preference.where(PushSettings.user_id == uid))).first()
            if current is not None:
                should_send = current.enabled == 1 and (filter_func is None or filter_func(current))
            elif configured:
                should_send = False
        # Release the transaction before network I/O and retain loaded user fields.
        if user is not None:
            db.expunge(user)
        await db.rollback()
        if not should_send:
            continue
        if await notify_user(user, message):
            if delivered is not None:
                delivered.append(uid)
        else:
            ok = False
    return ok


async def _notify_recipients_filtered(
    db,
    device_type: str,
    device_id: int,
    message: str,
    filter_func,
    delivered: list | None = None,
    skip_ids: set | None = None,
):
    """Send only to explicitly configured recipients who pass the job filter."""
    if device_type not in {"cams", "switches"}:
        return True
    return await _notify_recipients(
        db, device_type, device_id, message, delivered=delivered, skip_ids=skip_ids,
        filter_func=filter_func,
    )


async def notify_cam_recipients(db, cam: Cam, message: str, *,
                                delivered: list | None = None, skip_ids: set | None = None) -> bool:
    return await _notify_recipients(db, "cams", cam.id, message,
                                    delivered=delivered, skip_ids=skip_ids)


async def notify_switch_recipients(db, switch: Switch, message: str, *,
                                   delivered: list | None = None, skip_ids: set | None = None) -> bool:
    return await _notify_recipients(db, "switches", switch.id, message,
                                    delivered=delivered, skip_ids=skip_ids)


async def _notify_cam_id(cam_id: int, message: str):
    async with AsyncSessionLocal() as db:
        cam = await db.get(Cam, cam_id)
        if cam is not None:
            await notify_cam_recipients(db, cam, message)


async def _notify_switch_id(switch_id: int, message: str):
    async with AsyncSessionLocal() as db:
        switch = await db.get(Switch, switch_id)
        if switch is not None:
            await notify_switch_recipients(db, switch, message)


def notify_cam_background(cam_id: int, message: str) -> None:
    """Fire-and-forget: opens its own DB session so it outlives the request that triggered it."""
    asyncio.create_task(_notify_cam_id(cam_id, message))


def notify_switch_background(switch_id: int, message: str) -> None:
    """Fire-and-forget: opens its own DB session so it outlives the request that triggered it."""
    asyncio.create_task(_notify_switch_id(switch_id, message))
