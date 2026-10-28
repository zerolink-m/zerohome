"""Background worker that periodically pulls device inventories from configured
third-party clouds (currently only Tuya) and materialises each device as a
`Switch` row with `added_by='auto'`.

Design notes:
- One iteration walks every enabled `Connection` sequentially. Providers can
  hang for the full per-request timeout (see `TuyaCloudClient`'s default
  timeout) but that's bounded — no need to fan out with `asyncio.gather`, and
  sequential access keeps DB pressure trivial.
- The sync is *idempotent per (connection_id, external_id)*: existing auto-rows
  are upserted, missing ones deleted, extras created. User-added switches
  (`added_by='user'`) are never touched even if their `controlmeta.device_id`
  happens to match an external_id — we only trust rows tagged 'auto'.
- Failure isolation: a provider error only marks that one Connection's
  `last_sync_error` (and leaves `last_synced_at` untouched, so consumers can
  tell "sync never succeeded" from "sync succeeded then broke"). Its existing
  auto-switches stay in place — losing device visibility during a transient
  outage would be worse than a slightly stale name/state list.
- Category filter: Tuya returns everything (bulbs, cameras, sensors) on the
  same account; we only ingest categories `kg` (switch) and `cz` (socket) —
  the two categories the current `switches` table is designed for.
- Cleanup of polymorphic invitations/sharings on delete: no FK cascade exists
  from device_id to switches.id (the sharings/invitations tables are
  polymorphic across cams+switches), so we clean them explicitly here.
"""

from app.services.connection_sync.helpers import (  # noqa: F401
    _TUYA_ALLOWED_CATEGORIES,
    _aggregate_state_from_children,
    _cleanup_orphan_shares,
    _switch_dp_codes,
    _tuya_child_controlmeta,
    _tuya_master_controlmeta,
)
from app.services.connection_sync.tuya_sync import _sync_tuya_connection  # noqa: F401
from app.services.connection_sync.runner import _loop, _sync_one, _sync_once, start, sync_connection_now  # noqa: F401
