from __future__ import annotations

import asyncio
import httpx
import logging
import time
from dataclasses import dataclass

from onvif import ONVIFCamera

from app.core.codes import (
    BAD_REQUEST, EXTERNAL_API_UNAVAILABLE, ONVIF_CAMERA_AUTH_REJECTED,
    ONVIF_CAMERA_TIMEOUT, PTZ_NOT_CONFIGURED,
)
from app.core.config import settings
from app.core.errors import AppError
from app.models.cams import Cam

logger = logging.getLogger(__name__)


@dataclass
class _Range:
    lo: float
    hi: float


@dataclass
class _PTZContext:
    camera: ONVIFCamera
    media_service: object
    profile_token: str
    ptz_service: object | None = None
    pan_range: _Range | None = None
    tilt_range: _Range | None = None
    zoom_range: _Range | None = None


_cache: dict[int, _PTZContext] = {}
_cache_locks: dict[int, asyncio.Lock] = {}


class _AuthDenied(AppError):
    """The camera rejected the ONVIF credentials (or locked the account)."""


class _CameraTimeout(AppError):
    """An ONVIF request timed out, including while initializing PTZ."""


# cam.id -> monotonic deadline until which the camera is not contacted over ONVIF
_auth_denied: dict[int, float] = {}

_AUTH_MARKERS = (
    "authorization",
    "not authorized",
    "notauthorized",
    "unauthorized",
    "locked",
    "wrong username",
    "wrong password",
    "authentication",
)
_AUTH_MESSAGE = "Камера отклонила учётные данные ONVIF (неверный логин/пароль или камера заблокирована)"


def _now() -> float:
    return time.monotonic()


def _is_auth_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if status in (401, 403):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_MARKERS)


def _auth_denied_error() -> AppError:
    return _AuthDenied(ONVIF_CAMERA_AUTH_REJECTED, _AUTH_MESSAGE)


def _check_backoff(cam_id: int) -> None:
    until = _auth_denied.get(cam_id)
    if until is None:
        return
    if _now() >= until:
        _auth_denied.pop(cam_id, None)
        return
    raise _auth_denied_error()


def _mark_auth_denied(cam_id: int) -> None:
    _auth_denied[cam_id] = _now() + settings.ONVIF_AUTH_BACKOFF_SECONDS
    _cache.pop(cam_id, None)


def _translate(cam_id: int | None, exc: Exception) -> AppError:
    """Map an ONVIF/transport exception to an AppError (never leaks URLs or credentials)."""
    if _is_auth_error(exc):
        if cam_id is not None:
            _mark_auth_denied(cam_id)
        logger.warning(f"[ONVIF] Cam {cam_id}: камера отклонила авторизацию ({type(exc).__name__})")
        return _auth_denied_error()
    logger.warning(f"[ONVIF] Cam {cam_id}: ошибка ONVIF ({type(exc).__name__}: {exc})")
    return AppError(EXTERNAL_API_UNAVAILABLE, f"Ошибка связи с камерой по ONVIF ({type(exc).__name__})")


async def _with_timeout(coro, cam_id: int | None = None):
    try:
        result = await asyncio.wait_for(coro, timeout=settings.ONVIF_TIMEOUT_SECONDS)
    except AppError:
        raise
    except asyncio.TimeoutError as exc:
        raise _CameraTimeout(ONVIF_CAMERA_TIMEOUT, "Камера не отвечает (ONVIF)") from exc
    except Exception as exc:
        raise _translate(cam_id, exc) from exc
    if cam_id is not None:
        _auth_denied.pop(cam_id, None)
    return result


def _cache_lock(cam_id: int) -> asyncio.Lock:
    lock = _cache_locks.get(cam_id)
    if lock is None:
        lock = asyncio.Lock()
        _cache_locks[cam_id] = lock
    return lock


def invalidate(cam_id: int) -> None:
    _cache.pop(cam_id, None)
    _auth_denied.pop(cam_id, None)


async def _build_context(cam: Cam) -> _PTZContext:
    try:
        camera = ONVIFCamera(
            cam.control_addr,
            cam.control_port,
            cam.control_username,
            cam.control_password,
        )
    except Exception as exc:
        raise _translate(cam.id, exc) from exc
    await _with_timeout(camera.update_xaddrs(), cam.id)

    media = await _with_timeout(camera.create_media_service(), cam.id)
    profiles = await _with_timeout(media.GetProfiles(), cam.id)
    if not profiles:
        raise AppError(
            PTZ_NOT_CONFIGURED,
            "Камера не вернула ни одного медиапрофиля (ONVIF)",
        )
    profile = profiles[0]

    ptz_service = None
    pan_range = None
    tilt_range = None
    zoom_range = None

    if profile.PTZConfiguration is not None:
        try:
            ptz = await _with_timeout(camera.create_ptz_service(), cam.id)
            options = await _with_timeout(
                ptz.GetConfigurationOptions(
                    {"ConfigurationToken": profile.PTZConfiguration.token}
                ),
                cam.id,
            )

            pt_spaces = options.Spaces.AbsolutePanTiltPositionSpace if options.Spaces else None
            zoom_spaces = options.Spaces.AbsoluteZoomPositionSpace if options.Spaces else None
            if pt_spaces and zoom_spaces:
                pt_space = pt_spaces[0]
                zoom_space = zoom_spaces[0]
                ptz_service = ptz
                pan_range = _Range(pt_space.XRange.Min, pt_space.XRange.Max)
                tilt_range = _Range(pt_space.YRange.Min, pt_space.YRange.Max)
                zoom_range = _Range(zoom_space.XRange.Min, zoom_space.XRange.Max)
        except (_AuthDenied, _CameraTimeout):
            raise
        except Exception as exc:
            logger.warning(f"[ONVIF] Cam {cam.id}: не удалось инициализировать PTZ ({exc})")

    return _PTZContext(
        camera=camera,
        media_service=media,
        profile_token=profile.token,
        ptz_service=ptz_service,
        pan_range=pan_range,
        tilt_range=tilt_range,
        zoom_range=zoom_range,
    )


async def _get_context(cam: Cam) -> _PTZContext:
    _check_backoff(cam.id)
    ctx = _cache.get(cam.id)
    if ctx is not None:
        return ctx
    async with _cache_lock(cam.id):
        ctx = _cache.get(cam.id)
        if ctx is None:
            ctx = await _build_context(cam)
            _cache[cam.id] = ctx
        return ctx


async def get_status(cam: Cam) -> dict[str, float]:
    ctx = await _get_context(cam)
    if ctx.ptz_service is None:
        raise AppError(PTZ_NOT_CONFIGURED, "У камеры не настроен PTZ (ONVIF)")
    status = await _with_timeout(
        ctx.ptz_service.GetStatus({"ProfileToken": ctx.profile_token}),
        cam.id,
    )
    position = status.Position
    pan_tilt = position.PanTilt if position is not None else None
    zoom = position.Zoom if position is not None else None
    return {
        "pan": pan_tilt.x if pan_tilt is not None else 0.0,
        "tilt": pan_tilt.y if pan_tilt is not None else 0.0,
        "zoom": zoom.x if zoom is not None else 0.0,
    }


async def absolute_move(
    cam: Cam,
    pan: float | None,
    tilt: float | None,
    zoom: float | None,
) -> None:
    ctx = await _get_context(cam)
    if (
        ctx.ptz_service is None
        or ctx.pan_range is None
        or ctx.tilt_range is None
        or ctx.zoom_range is None
    ):
        raise AppError(PTZ_NOT_CONFIGURED, "У камеры не настроен PTZ (ONVIF)")
    current = await get_status(cam)

    target_pan = pan if pan is not None else current["pan"]
    target_tilt = tilt if tilt is not None else current["tilt"]
    target_zoom = zoom if zoom is not None else current["zoom"]

    for name, value, rng in (
        ("pan", target_pan, ctx.pan_range),
        ("tilt", target_tilt, ctx.tilt_range),
        ("zoom", target_zoom, ctx.zoom_range),
    ):
        if value < rng.lo or value > rng.hi:
            raise AppError(
                BAD_REQUEST,
                f"{name} вне диапазона [{rng.lo},{rng.hi}], поддерживаемого камерой",
            )

    await _with_timeout(
        ctx.ptz_service.AbsoluteMove(
            {
                "ProfileToken": ctx.profile_token,
                "Position": {
                    "PanTilt": {"x": target_pan, "y": target_tilt},
                    "Zoom": {"x": target_zoom},
                },
            }
        ),
        cam.id,
    )


async def get_snapshot_uri(cam: Cam) -> str:
    """Get snapshot URI from ONVIF camera."""
    logger.debug(f"[ONVIF] Cam {cam.id}: запрос snapshot URI")
    ctx = await _get_context(cam)
    snapshot_uri = await _with_timeout(
        ctx.media_service.GetSnapshotUri({"ProfileToken": ctx.profile_token}),
        cam.id,
    )
    if not snapshot_uri or not snapshot_uri.Uri:
        logger.error(f"[ONVIF] Cam {cam.id}: камера не поддерживает GetSnapshotUri")
        raise AppError(PTZ_NOT_CONFIGURED, "Камера не поддерживает GetSnapshotUri")
    logger.debug(f"[ONVIF] Cam {cam.id}: получен snapshot URI: {snapshot_uri.Uri}")
    return snapshot_uri.Uri


async def get_snapshot(cam: Cam) -> bytes:
    """Get JPEG snapshot from ONVIF camera."""
    logger.debug(f"[ONVIF] Cam {cam.id}: начало получения snapshot")
    uri = await get_snapshot_uri(cam)

    # Download snapshot via HTTP
    timeout = httpx.Timeout(settings.ONVIF_TIMEOUT_SECONDS)
    auth = httpx.DigestAuth(cam.control_username, cam.control_password)

    try:
        logger.debug(f"[ONVIF] Cam {cam.id}: HTTP GET запрос к snapshot URI")
        async with httpx.AsyncClient(timeout=timeout, auth=auth) as client:
            response = await client.get(uri)
        # the digest 401 challenge is handled by httpx; only the final response counts
        if response.status_code in (401, 403):
            _mark_auth_denied(cam.id)
            raise _auth_denied_error()
        response.raise_for_status()
        snapshot_data = response.content
    except AppError:
        raise
    except Exception as exc:
        logger.error(f"[ONVIF] Cam {cam.id}: ошибка HTTP запроса snapshot: {type(exc).__name__}")
        raise AppError(EXTERNAL_API_UNAVAILABLE, f"Ошибка получения snapshot ({type(exc).__name__})") from exc
    logger.debug(f"[ONVIF] Cam {cam.id}: получен snapshot через HTTP ({len(snapshot_data)} bytes, status={response.status_code})")
    return snapshot_data
