"""Local directory backend. Writes are atomic (temp file in the same directory + os.replace)."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from app.services.storage.base import validate_key


class LocalStorage:
    def __init__(self, root: str) -> None:
        self.root = root

    def _destination(self, key: str) -> Path:
        key = validate_key(key)
        root = Path(self.root).resolve()
        path = root.joinpath(*key.split("/"))
        if not path.is_relative_to(root):
            raise ValueError("invalid storage key")
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        del content_type  # local files are not tagged; callers still pass image/jpeg
        path = self._destination(key)
        await asyncio.to_thread(_atomic_write, path, data)

    async def get(self, key: str) -> bytes | None:
        path = self._destination(key)
        if not path.is_file():
            return None
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, key: str) -> None:
        path = self._destination(key)
        if path.is_file():
            await asyncio.to_thread(path.unlink)

    async def exists(self, key: str) -> bool:
        return self._destination(key).is_file()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".part-", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
