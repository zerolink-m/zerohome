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

from app.services.connection_sync.helpers import (
    _TUYA_ALLOWED_CATEGORIES,
    _aggregate_state_from_children,
    _cleanup_orphan_shares,
    _switch_dp_codes,
    _tuya_child_controlmeta,
    _tuya_master_controlmeta,
)


async def _sync_tuya_connection(db, conn: Connection) -> None:
    """Sync a single Tuya connection. Raises TuyaError on provider failures
    (or ValueError on misconfiguration like a missing tuya_uid); caller
    catches and records that on the Connection row without touching the
    auto-switch rows.

    Materialisation model:
        1 Tuya device → 1 master row (is_master=1) + N child rows (one per
        boolean switch_N DP). external_id is the Tuya device_id, identical
        for the master and all children; external_switch_channel = the DP
        code on children, NULL on master.
    """
    m = conn.meta or {}
    tuya_uid = m.get("tuya_uid")
    if not isinstance(tuya_uid, str) or not tuya_uid:
        # Misconfiguration, not a provider failure — surface as a plain
        # ValueError so `_sync_one`'s generic handler records it distinctly
        # from Tuya-side (TuyaError) failures.
        raise ValueError(
            "meta.tuya_uid не задан — синхронизация невозможна до его установки"
        )
    client = await tuya_client_pool.get_client(
        access_id=m["access_id"],
        access_secret=m["access_secret"],
        endpoint=m["endpoint"],
    )

    # Page through the full device list — the new client returns 50 per page
    # by default, so a single call would silently truncate accounts with more
    # than 50 devices (the old client had this exact latent bug).
    devices = []
    last_row_key: str | None = None
    while True:
        page, last_row_key = await client.list_user_devices(
            tuya_uid, last_row_key=last_row_key
        )
        devices.extend(page)
        if not last_row_key or not page:
            break

    # Filter to categories we support. Index by external_id.
    remote: dict[str, dict] = {}
    for d in devices:
        cat = d.category
        did = d.id
        if cat in _TUYA_ALLOWED_CATEGORIES and isinstance(did, str) and did:
            # Keep the raw device fields the downstream builders expect
            # (they read `.get("id")` / `.get("name")`), so hand them a dict.
            remote[did] = {"id": d.id, "name": d.name, "category": d.category}

    # Fetch DP list for every remote device up front. One HTTP call per
    # device, but we're already sequential over connections so this is
    # bounded. Cache-friendly (each device is small).
    dp_by_device: dict[str, list[str]] = {}
    status_by_device: dict[str, dict[str, bool]] = {}
    for ext_id in list(remote.keys()):
        try:
            spec = await client.get_device_specifications(ext_id)
            dp_by_device[ext_id] = _switch_dp_codes(spec)
        except TuyaError as exc:
            # A single-device spec failure shouldn't nuke the whole connection.
            # Drop the device — better than materialising channels we don't
            # know exist.
            logger.warning(
                "connection_id=%s device=%s specifications failed: %s",
                conn.id, ext_id, exc,
            )
            remote.pop(ext_id, None)
            continue
        try:
            items = await client.get_device_status(ext_id)
            status_by_device[ext_id] = {
                i.code: bool(i.value)
                for i in items
                if isinstance(i.code, str)
            }
        except TuyaError:
            status_by_device[ext_id] = {}

    existing = (
        await db.execute(
            select(Switch).where(
                Switch.connection_id == conn.id, Switch.added_by == "auto"
            )
        )
    ).scalars().all()
    # Key existing rows by (external_id, external_switch_channel):
    # channel=None for masters, "switch_N" for children.
    existing_by_key: dict[tuple[str, str | None], Switch] = {
        (s.external_id, s.external_switch_channel): s for s in existing
    }

    now = int(time.time())
    # Track which keys we've seen upstream this run — leftovers get deleted.
    seen_keys: set[tuple[str, str | None]] = set()

    for ext_id, d in remote.items():
        remote_name = d.get("name") or ""
        dp_codes = dp_by_device.get(ext_id) or []
        if not dp_codes:
            # Device has no boolean switch_N channels — nothing to control.
            # Skip entirely (don't create a master with no children).
            continue
        status = status_by_device.get(ext_id) or {}

        # ---- master ----
        master_key = (ext_id, None)
        seen_keys.add(master_key)
        master_meta = _tuya_master_controlmeta(conn, d, dp_codes)
        master = existing_by_key.get(master_key)
        # Aggregate master state from what the device reports right now
        # (OR across the DP codes we care about).
        agg_state = 1 if any(status.get(code) for code in dp_codes) else 0

        if master is None:
            master = Switch(
                ownuser=conn.uid,
                name=remote_name,
                enabled=0,
                notify=0,
                control_proto="tuya_cloud",
                controlmeta=master_meta,
                state=agg_state,
                state_updated_at=now,
                added_by="auto",
                connection_id=conn.id,
                external_id=ext_id,
                is_master=1,
                parent=None,
                external_switch_channel=None,
                added_at=now,
                updated_at=now,
            )
            db.add(master)
            # Flush so master.id is available for children's `parent` FK below.
            await db.flush()
        else:
            if master.controlmeta != master_meta:
                master.controlmeta = master_meta
                master.updated_at = now
            if not master.name and remote_name:
                master.name = remote_name
                master.updated_at = now
            if master.state != agg_state:
                master.state = agg_state
                master.state_updated_at = now

        # ---- children ----
        for idx, dp_code in enumerate(dp_codes, start=1):
            child_key = (ext_id, dp_code)
            seen_keys.add(child_key)
            child_meta = _tuya_child_controlmeta(conn, d, dp_code)
            channel_state = 1 if status.get(dp_code) else 0

            # Default child name: append channel index only for multi-channel
            # devices; single-channel keeps the plain device name.
            default_child_name = (
                remote_name if len(dp_codes) == 1 else f"{remote_name} — {idx}"
            )

            child = existing_by_key.get(child_key)
            if child is None:
                db.add(
                    Switch(
                        ownuser=conn.uid,
                        name=default_child_name,
                        enabled=0,
                        notify=0,
                        control_proto="tuya_cloud",
                        controlmeta=child_meta,
                        state=channel_state,
                        state_updated_at=now,
                        added_by="auto",
                        connection_id=conn.id,
                        external_id=ext_id,
                        is_master=0,
                        parent=master.id,
                        external_switch_channel=dp_code,
                        added_at=now,
                        updated_at=now,
                    )
                )
            else:
                if child.controlmeta != child_meta:
                    child.controlmeta = child_meta
                    child.updated_at = now
                if not child.name and default_child_name:
                    child.name = default_child_name
                    child.updated_at = now
                if child.state != channel_state:
                    child.state = channel_state
                    child.state_updated_at = now
                # Repair parent link if the master got recreated.
                if child.parent != master.id:
                    child.parent = master.id
                    child.updated_at = now

    # Delete rows whose (device, channel) no longer exists upstream.
    # Delete children first so master delete doesn't try to cascade into
    # already-detached FK rows. Handling is polymorphic-shares-safe.
    to_delete = [
        row for key, row in existing_by_key.items() if key not in seen_keys
    ]
    to_delete.sort(key=lambda r: 0 if r.is_master == 0 else 1)
    for row in to_delete:
        await _cleanup_orphan_shares(db, row.id)
        await db.delete(row)
