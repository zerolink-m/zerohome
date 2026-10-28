from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.connection import Connection
from app.models.invitation import Invitation
from app.models.sharing import Sharing
from app.models.switch import Switch
from app.services import tuya_client_pool
from app.services.tuya import Function, Specifications, TuyaError

logger = logging.getLogger("app.services.connection_sync")

# Tuya categories we materialise into `switches`. Anything else (lights,
# sensors, cameras via Tuya, etc.) is skipped silently — future device types
# will get their own tables and their own sync routines.
_TUYA_ALLOWED_CATEGORIES = frozenset({"kg", "cz"})


async def _cleanup_orphan_shares(db, device_id: int) -> None:
    """Remove invitations/sharings for a switch we're about to delete.

    Polymorphic (device_type, device_id) has no FK to `switches.id`, so no
    cascade fires. Doing this before the switch itself gets deleted keeps the
    tables in sync even if the switch DELETE later fails and rolls back —
    they'd just point at a nonexistent device_id, same as if the sharing
    row was never created."""
    await db.execute(
        delete(Invitation).where(
            Invitation.device_type == "switches", Invitation.device_id == device_id
        )
    )
    await db.execute(
        delete(Sharing).where(
            Sharing.device_type == "switches", Sharing.device_id == device_id
        )
    )


def _tuya_master_controlmeta(conn: Connection, device: dict, dp_codes: list[str]) -> dict:
    """Master row's controlmeta: the full list of channel DP codes lives here
    so `switch_client.set_state` on the master fires a batch of all channels
    in a single Tuya API call."""
    m = conn.meta or {}
    return {
        "access_id": m.get("access_id"),
        "access_secret": m.get("access_secret"),
        "endpoint": m.get("endpoint"),
        "device_id": device.get("id"),
        "dp_codes": dp_codes,
    }


def _tuya_child_controlmeta(conn: Connection, device: dict, dp_code: str) -> dict:
    """Child row's controlmeta: just one DP code — the channel this row
    represents. Everything else mirrors the master."""
    m = conn.meta or {}
    return {
        "access_id": m.get("access_id"),
        "access_secret": m.get("access_secret"),
        "endpoint": m.get("endpoint"),
        "device_id": device.get("id"),
        "dp_code": dp_code,
    }


def _switch_dp_codes(spec: Specifications) -> list[str]:
    """Extracts only the boolean `switch_*` DP codes from a device
    specification response — those are the channels we materialise as
    children. Order preserved so channel numbering matches upstream.

    Anything else (`switch_backlight`, `switch_inching`, `switch_usb1`,
    `countdown_*`, ...) is deliberately excluded — the master DP `switch`
    (aggregate) is also excluded here since we synthesize the master row
    ourselves regardless of whether it exists upstream.
    """
    codes: list[str] = []
    for f in spec.functions:
        if not isinstance(f, Function):
            continue
        code = f.code
        if not isinstance(code, str):
            continue
        if f.type != "Boolean":
            continue
        # match "switch_1", "switch_2", ..., but NOT "switch_backlight",
        # "switch_inching", "switch_usb1", or the bare aggregate "switch".
        if code.startswith("switch_") and code[len("switch_"):].isdigit():
            codes.append(code)
    return codes


def _aggregate_state_from_children(children: list) -> int:
    """Master state = OR over its children — on if any channel is on."""
    return 1 if any(c.state == 1 for c in children) else 0
