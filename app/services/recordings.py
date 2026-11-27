from __future__ import annotations

import os

from app.core.codes import BAD_REQUEST, OBJECT_NOT_FOUND, RECORDING_NOT_CONFIGURED
from app.core.errors import AppError
from app.models.cams import Cam


SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".mp4": "mp4",
}

MEDIA_TYPE_BY_FORMAT: dict[str, str] = {
    "mp4": "video/mp4",
}


def validate_record_meta(record_type: str, record_meta) -> None:
    """record_meta's shape (CamRecordingMeta) is already enforced by the schema layer —
this only checks the filesystem-dependent bit that Pydantic can't validate."""
    if record_type != "local":
        raise AppError(BAD_REQUEST, f"Неизвестный record_type: {record_type}")

    local = record_meta.get("local") if isinstance(record_meta, dict) else None

    if not (
        isinstance(local, dict)
        and isinstance(local.get("path"), str)
        and local["path"]
    ):
        raise AppError(
            BAD_REQUEST,
            "record_meta.local.path обязателен и должен быть непустой строкой",
        )

    # Users trusted with allowrecord may select any directory readable by the service.
    path = local["path"]
    if not (os.path.isdir(path) and os.access(path, os.R_OK)):
        raise AppError(BAD_REQUEST, f"Путь record_meta.local.path недоступен: {path}")


def _local_dir(cam: Cam) -> str:
    if cam.record != 1 or cam.record_type is None:
        raise AppError(RECORDING_NOT_CONFIGURED, "Запись не настроена для этой камеры")

    meta = cam.record_meta if isinstance(cam.record_meta, dict) else None
    local = meta.get("local") if meta is not None else None
    path = local.get("path") if isinstance(local, dict) else None

    if not path:
        raise AppError(RECORDING_NOT_CONFIGURED, "Не задан путь record_meta.local.path")

    return path


def list_recordings(
    cam: Cam,
    offset: int,
    limit: int,
    from_ts: int | None,
    to_ts: int | None,
) -> tuple[list[dict], int]:
    base_dir = _local_dir(cam)

    items: list[dict] = []

    with os.scandir(base_dir) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue

            ext = os.path.splitext(entry.name)[1].lower()
            fmt = SUPPORTED_EXTENSIONS.get(ext)
            if fmt is None:
                continue

            stat_result = entry.stat(follow_symlinks=False)
            started_at = int(stat_result.st_mtime)

            if from_ts is not None and started_at < from_ts:
                continue
            if to_ts is not None and started_at > to_ts:
                continue

            items.append(
                {
                    "id": entry.name,
                    "filename": entry.name,
                    "format": fmt,
                    "started_at": started_at,
                    "size_bytes": stat_result.st_size,
                }
            )

    items.sort(key=lambda item: item["started_at"], reverse=True)

    total = len(items)
    page = items[offset : offset + limit]
    return page, total


def get_recording(cam: Cam, recording_id: str) -> dict:
    full_path, _ = resolve_file(cam, recording_id)
    ext = os.path.splitext(full_path)[1].lower()
    stat_result = os.stat(full_path)
    return {
        "id": recording_id,
        "filename": recording_id,
        "format": SUPPORTED_EXTENSIONS[ext],
        "started_at": int(stat_result.st_mtime),
        "size_bytes": stat_result.st_size,
    }


def resolve_file(cam: Cam, recording_id: str) -> tuple[str, str]:
    if (
        not recording_id
        or recording_id in (".", "..")
        or "/" in recording_id
        or "\\" in recording_id
    ):
        raise AppError(OBJECT_NOT_FOUND, "Recording not found")

    base_dir = _local_dir(cam)
    base_real = os.path.realpath(base_dir)
    full_path = os.path.realpath(os.path.join(base_dir, recording_id))

    if os.path.commonpath([base_real, full_path]) != base_real:
        raise AppError(OBJECT_NOT_FOUND, "Recording not found")

    ext = os.path.splitext(full_path)[1].lower()
    fmt = SUPPORTED_EXTENSIONS.get(ext)

    if fmt is None or not os.path.isfile(full_path):
        raise AppError(OBJECT_NOT_FOUND, "Recording not found")

    return full_path, MEDIA_TYPE_BY_FORMAT[fmt]
