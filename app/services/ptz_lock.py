from __future__ import annotations

import time


_TTL_SECONDS = 3.0

_locks: dict[int, float] = {}


def is_locked(cam_id: int) -> bool:
    expiry = _locks.get(cam_id)
    return expiry is not None and expiry > time.monotonic()


def acquire(cam_id: int) -> None:
    _locks[cam_id] = time.monotonic() + _TTL_SECONDS
