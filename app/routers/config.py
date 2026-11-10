"""Config (virtual object): frontend-version check, supported streams and server time."""
import time

from fastapi import APIRouter, Depends, Query

from app.core.codes import (
    BAD_REQUEST,
    FRONTEND_UPDATE_AVAILABLE,
    FRONTEND_UPDATE_NEEDED,
    SUCCESS,
)
from app.core.config import settings
from app.core.errors import AppError
from app.core.rate_limit import rate_limit
from app.core.response import success
from app.core.validation import field_error
from app.schemas.config import ConfigOut, ServerTimeOut
from app.services import frontend_version

router = APIRouter(prefix="/v1/config", tags=["config"])

# legacy codes stored in frontend_versions.json -> CursedJSON codes
_VERSION_CODES = {0: SUCCESS, 5: FRONTEND_UPDATE_AVAILABLE, 67: FRONTEND_UPDATE_NEEDED}


@router.get("/frontend")
async def get_frontend_config():
    return success({"config": ConfigOut(version=settings.VERSION).model_dump(exclude_none=True)})


@router.get("/time", dependencies=[Depends(rate_limit("config_time", capacity=20, per_minute=120))])
async def get_time():
    """Current server time, unix timestamp UTC (no auth required)."""
    return success(ServerTimeOut(time=int(time.time())).model_dump())


@router.get("", dependencies=[Depends(rate_limit("config", capacity=20, per_minute=120))])
async def get_config(v: str | None = Query(None, description="Client version: vMAJOR.MINOR.PATCH-Channel")):
    """Frontend version check. 000 up to date, 020 update available (data returned),
    063 update required (no data), 040 malformed v."""
    if v is None:
        raise AppError(FRONTEND_UPDATE_NEEDED, "Требуется обновление фронтенда")
    client = frontend_version.parse_client_version(v)
    if client is None:
        raise AppError(
            BAD_REQUEST,
            "Параметр v имеет неверный формат",
            query={"v": field_error("must match vMAJOR.MINOR.PATCH-Release|Beta|Alpha")},
        )
    legacy = frontend_version.resolve_version_code(client)
    code = FRONTEND_UPDATE_NEEDED if legacy is None else _VERSION_CODES.get(legacy, SUCCESS)
    if code == FRONTEND_UPDATE_NEEDED:
        raise AppError(FRONTEND_UPDATE_NEEDED, "Требуется обновление фронтенда")
    return success(
        {"config": ConfigOut(version=v, actual_version=settings.VERSION).model_dump()},
        code=code,
    )


@router.get("/frontend/supported_streams")
async def get_supported_streams():
    """Get list of supported streaming formats (no auth required)"""
    streams = [
        {
            "format": "fmp4",
            "enabled": True,
            "readonly": True,
            "name": "Fragmented MP4",
            "description": "Low latency streaming via HTTP",
            "latency": "2-5s",
            "endpoint": "/v1/cams/{id}/stream",
            "mime_type": "video/mp4",
            "features": ["low_latency", "seek", "pause"]
        }
    ]

    if settings.HLS_ENABLED:
        streams.append({
            "format": "hls",
            "enabled": True,
            "readonly": False,
            "name": "HLS (HTTP Live Streaming)",
            "description": "Adaptive streaming for wide compatibility",
            "latency": "6-30s",
            "endpoint": "/v1/cams/{id}/hls/playlist.m3u8",
            "mime_type": "application/vnd.apple.mpegurl",
            "features": ["adaptive_bitrate", "wide_compatibility", "ios_native"]
        })

    if settings.WEBRTC_ENABLED:
        # Check if STUN server is configured
        if settings.WEBRTC_STUN_SERVER:
            streams.append({
                "format": "webrtc",
                "enabled": True,
                "readonly": False,
                "name": "WebRTC",
                "description": "Ultra low latency P2P streaming",
                "latency": "<1s",
                "endpoint": "/v1/cams/{id}/webrtc",
                "protocol": "websocket",
                "features": ["ultra_low_latency", "p2p"]
            })

    return success({"streams": streams})
