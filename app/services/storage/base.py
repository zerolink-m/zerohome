"""Storage backend contract. Keys are relative, like ``motion/<cam_id>/<filename>``."""
from __future__ import annotations

from typing import Protocol


def validate_key(key: str) -> str:
    """Reject absolute keys and any ``..`` segment. The key is returned unchanged."""
    if not isinstance(key, str) or not key or key.startswith(("/", "\\")) or "\\" in key or "\x00" in key:
        raise ValueError("invalid storage key")
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError("invalid storage key")
    return key


class Storage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes | None: ...

    async def delete(self, key: str) -> None:
        """Delete a key idempotently; an already-missing object is success."""
        ...

    async def exists(self, key: str) -> bool: ...
