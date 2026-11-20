from __future__ import annotations

import asyncio
import logging
import os

import httpx
from sqlalchemy import func, select

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


def _without(users, skip_ids):
    if not skip_ids:
        return list(users)
    return [user for user in users if getattr(user, "id", None) not in skip_ids]


async def _notify_recipients(db, device_type: str, device_id: int, owner_id: int, message: str,
                             delivered: list | None = None, skip_ids: set | None = None):
    """Notifies users who have push_settings.enabled=1 for this device."""
    # Get all push_settings for this device where enabled=1
    stmt = select(PushSettings).where(
        PushSettings.device_type == device_type,
        PushSettings.device_id == device_id,
        PushSettings.enabled == 1,
    )
    result = await db.execute(stmt)
    push_settings_list = result.scalars().all()

    if not push_settings_list:
        # Fallback: if no push_settings exist, use old behavior (owner + invited)
        owner = await db.get(User, owner_id)
        recipients = [owner] if owner is not None else []

        accepted_emails = (
            (
                await db.execute(
                    select(Invitation.email).where(
                        Invitation.device_type == device_type,
                        Invitation.device_id == device_id,
                        Invitation.status == "accepted",
                    )
                )
            )
            .scalars()
            .all()
        )

        if accepted_emails:
            invitees = (
                (
                    await db.execute(
                        select(User).where(
                            func.lower(User.email).in_([e.lower() for e in accepted_emails])
                        )
                    )
                )
                .scalars()
                .all()
            )
            recipients.extend(invitees)

        recipients = _without(recipients, skip_ids)
        if not recipients:
            return True
        return await _notify_users(recipients, message, delivered)
    # New behavior: notify based on push_settings
    user_ids = [ps.user_id for ps in push_settings_list]
    stmt_users = select(User).where(User.id.in_(user_ids))
    result_users = await db.execute(stmt_users)
    users = _without(result_users.scalars().all(), skip_ids)
    if not users:
        return True
    return await _notify_users(users, message, delivered)


async def _notify_recipients_filtered(
    db,
    device_type: str,
    device_id: int,
    owner_id: int,
    message: str,
    filter_func=None,
    delivered: list | None = None,
    skip_ids: set | None = None,
):
    """Notifies users with additional filtering based on push_settings.

    filter_func: function(push_settings) -> bool to filter which users to notify
    """
    # Get all push_settings for this device where enabled=1
    stmt = select(PushSettings).where(
        PushSettings.device_type == device_type,
        PushSettings.device_id == device_id,
        PushSettings.enabled == 1,
    )
    result = await db.execute(stmt)
    push_settings_list = result.scalars().all()

    # Apply filter if provided
    if filter_func:
        push_settings_list = [ps for ps in push_settings_list if filter_func(ps)]

    if not push_settings_list:
        return True

    # Get users
    user_ids = [ps.user_id for ps in push_settings_list]
    stmt_users = select(User).where(User.id.in_(user_ids))
    result_users = await db.execute(stmt_users)
    users = _without(result_users.scalars().all(), skip_ids)
    if not users:
        return True
    return await _notify_users(users, message, delivered)


async def _notify_users(users, message: str, delivered: list | None = None) -> bool:
    ok = True
    for user in users:
        # Do not short-circuit: one failure must not skip the remaining recipients.
        if not await notify_user(user, message):
            ok = False
            continue
        if delivered is not None:
            uid = getattr(user, "id", None)
            if isinstance(uid, int) and not isinstance(uid, bool):
                delivered.append(uid)
    return ok


async def notify_cam_recipients(db, cam: Cam, message: str, *,
                                delivered: list | None = None, skip_ids: set | None = None) -> bool:
    return await _notify_recipients(db, "cams", cam.id, cam.ownuser, message,
                                    delivered=delivered, skip_ids=skip_ids)


async def notify_switch_recipients(db, switch: Switch, message: str, *,
                                   delivered: list | None = None, skip_ids: set | None = None) -> bool:
    return await _notify_recipients(db, "switches", switch.id, switch.ownuser, message,
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
