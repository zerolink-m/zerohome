from __future__ import annotations

import asyncio
import logging

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

_SEND_TIMEOUT_SECONDS = 5.0


async def _send_telegram(bot_token, telegram_id, message) -> None:
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
                resp.text[:200],
            )
    except httpx.HTTPError as exc:
        logger.warning("telegram push failed: %s", exc)


async def notify_user(user, message):
    """Best-effort push delivery — never raises, never blocks the caller's transaction."""
    if user.push == "telegram":
        meta = user.pushmeta if isinstance(user.pushmeta, dict) else {}
        telegram = meta.get("telegram") if isinstance(meta.get("telegram"), dict) else None
        if not telegram:
            return
        bot_token = telegram.get("bot_token")
        telegram_id = telegram.get("telegram_id")
        if not bot_token or not isinstance(telegram_id, int):
            return
        await _send_telegram(bot_token, telegram_id, message)


async def _notify_recipients(db, device_type: str, device_id: int, owner_id: int, message: str):
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

        for user in recipients:
            await notify_user(user, message)
    else:
        # New behavior: notify based on push_settings
        user_ids = [ps.user_id for ps in push_settings_list]
        stmt_users = select(User).where(User.id.in_(user_ids))
        result_users = await db.execute(stmt_users)
        users = result_users.scalars().all()

        for user in users:
            await notify_user(user, message)


async def _notify_recipients_filtered(
    db,
    device_type: str,
    device_id: int,
    owner_id: int,
    message: str,
    filter_func=None,
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
        return

    # Get users
    user_ids = [ps.user_id for ps in push_settings_list]
    stmt_users = select(User).where(User.id.in_(user_ids))
    result_users = await db.execute(stmt_users)
    users = result_users.scalars().all()

    for user in users:
        await notify_user(user, message)


async def notify_cam_recipients(db, cam: Cam, message: str):
    await _notify_recipients(db, "cams", cam.id, cam.ownuser, message)


async def notify_switch_recipients(db, switch: Switch, message: str):
    await _notify_recipients(db, "switches", switch.id, switch.ownuser, message)


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
