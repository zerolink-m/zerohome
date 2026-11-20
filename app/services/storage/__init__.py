"""Photo storage. ``local`` (default) keeps today's single-node layout; ``s3`` does not fall back."""
from __future__ import annotations

import os

from app.core.config import settings
from app.services.storage.base import Storage, validate_key
from app.services.storage.local import LocalStorage
from app.services.storage.s3 import S3Storage

_S3_REQUIRED = ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY")


def legacy_motion_path(cam_id: int, filename: str) -> str:
    """Where motion photos lived before photo_key: ``<MOTION_FILES_DIR>/<cam_id>/<filename>``."""
    name = os.path.basename((filename or "").replace("\\", "/"))
    return os.path.join(settings.motion_files_path, str(int(cam_id)), name)


def get_storage() -> Storage:
    backend = (settings.STORAGE_BACKEND or "").strip().lower()
    if backend == "local":
        return LocalStorage(settings.motion_files_path)
    if backend == "s3":
        missing = [name for name in _S3_REQUIRED if not str(getattr(settings, name) or "").strip()]
        if missing:
            raise RuntimeError(
                "STORAGE_BACKEND=s3 requires non-empty settings: "
                + ", ".join(missing)
                + ". Refusing to start; not falling back to local storage."
            )
        return S3Storage(
            settings.S3_ENDPOINT.strip(),
            settings.S3_BUCKET.strip(),
            settings.S3_ACCESS_KEY.strip(),
            settings.S3_SECRET_KEY.strip(),
            settings.S3_REGION.strip(),
        )
    raise RuntimeError(f"STORAGE_BACKEND must be 'local' or 's3', got {settings.STORAGE_BACKEND!r}")


__all__ = ["LocalStorage", "S3Storage", "Storage", "get_storage", "legacy_motion_path", "validate_key"]
