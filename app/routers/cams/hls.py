from urllib.parse import quote, urlencode

from fastapi import Depends, Header, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import BAD_REQUEST, FILE_NOT_FOUND, OBJECT_NOT_FOUND, SERVER_ERROR
from app.core.config import settings
from app.core.errors import AppError
from app.core.introspection import supports
from app.core.rate_limit import PointBudget
from app.db.session import get_db
from app.deps.auth import AccessContext, require_access_qs, resolve_stream_access
from app.models.cams import Cam
from app.services import hls_manager, ufanet_media
from app.services.cluster import view_proxy
from app.services.cluster.identity import worker_enabled

from app.routers.cams._common import _LONG_CACHE_HEADERS, _NO_CACHE_HEADERS, _get_cam_for_access, _get_visible_cam, router



HLS_BUDGET = PointBudget("cams_hls", capacity=50, per_minute=50)


async def _hls_access(
    id: int,
    request: Request,
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> AccessContext | None:
    # A trusted internal hop was already authorized by the node that received
    # the client. Clustering off never takes this branch (trusted_proxied is false).
    if view_proxy.trusted_proxied(request):
        return None
    return await resolve_stream_access(id, request, authorization, token, db)


async def _cam_for_hls(id: int, ctx: AccessContext | None, db: AsyncSession) -> Cam:
    if ctx is not None:
        return await _get_cam_for_access(id, ctx, db)
    cam = await db.get(Cam, id)
    if cam is None:
        raise AppError(OBJECT_NOT_FOUND, "Camera not found")
    return cam


@router.get("/{id}/hls/playlist.m3u8")
@supports("streaming")
async def get_hls_playlist(
    id: int,
    request: Request,
    ctx: AccessContext | None = Depends(_hls_access),
    db: AsyncSession = Depends(get_db),
):
    if not settings.HLS_ENABLED:
        raise AppError(BAD_REQUEST, "HLS стриминг отключен")

    cam = await _cam_for_hls(id, ctx, db)
    if cam.connection_resource_id is not None:
        if ctx is None:
            ctx = await resolve_stream_access(id, request, request.headers.get('authorization'), request.query_params.get('token'), db)
            cam = await _get_cam_for_access(id, ctx, db)
        await HLS_BUDGET.charge(request, 1)
        text, base, host = await ufanet_media.playlist(db, cam)
        try:
            token = request.query_params.get("token")
            suffix = f"?token={quote(token, safe='')}" if token else ""
            body = ufanet_media.rewrite(text, base, host, lambda key: f"u-{key}.ts{suffix}", protected_cam=id)
        except (KeyError, ValueError) as exc:
            raise AppError(SERVER_ERROR, "Unsupported Ucams playlist") from exc
        return Response(content=body, media_type="application/vnd.apple.mpegurl", headers=_NO_CACHE_HEADERS)
    if ctx is not None:
        await HLS_BUDGET.charge(request, 1)
        target = await view_proxy.view_target(db, cam.id)
        if target is not None:
            return await view_proxy.proxy_http(request, target)

    # Apply node-local policy only after routing: an ingress node with HLS
    # disabled locally must still proxy to the node that owns this camera.
    if not worker_enabled(settings, "hls"):
        raise AppError(BAD_REQUEST, "HLS стриминг отключен на этой ноде")

    try:
        session = await hls_manager.get_or_create_session(cam)
    except hls_manager.HLSStreamError as exc:
        raise AppError(SERVER_ERROR, str(exc))

    # Touch session to update last access time
    session.touch()

    # Schedule idle stop (will be cancelled if accessed again)
    hls_manager.schedule_idle_stop(cam.id)

    playlist = session.playlist_text()
    if not playlist:
        raise AppError(SERVER_ERROR, "HLS playlist не найден")

    # hls.js resolves segment URLs relative to the playlist and does not carry
    # its query string over, so the segments would be requested without a token.
    # Rewrite each segment entry to carry the same credential.
    query_token = request.query_params.get("token")
    if query_token:
        suffix = f"?token={quote(query_token, safe='')}"
        lines = []
        for line in playlist.splitlines():
            entry = line.strip()
            if entry and not entry.startswith("#"):
                line = f"{entry}{suffix}"
            lines.append(line)
        body = "\n".join(lines) + "\n"
    else:
        body = playlist

    return Response(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers=_NO_CACHE_HEADERS,
    )


@router.get("/{id}/hls/archive/playlist.m3u8")
async def get_ufanet_archive_playlist(
    id: int, request: Request, start: int = Query(..., ge=1),
    duration: int = Query(..., ge=1, le=86400),
    payload=Depends(require_access_qs), db: AsyncSession = Depends(get_db),
):
    if not settings.HLS_ENABLED:
        raise AppError(BAD_REQUEST, "HLS стриминг отключен")
    cam = await _get_visible_cam(id, payload, db)
    if cam.connection_resource_id is None:
        raise AppError(OBJECT_NOT_FOUND, "Cloud archive not available")
    await HLS_BUDGET.charge(request, 1)
    text, base, host = await ufanet_media.playlist(db, cam, start, duration)
    params = {'start': start, 'duration': duration}
    if request.query_params.get('token'):
        params['token'] = request.query_params['token']
    suffix = '?' + urlencode(params)
    try:
        body = ufanet_media.rewrite(text, base, host, lambda key: f"resource/{key}{suffix}", protected_cam=id, scope=f'archive:{start}:{duration}')
    except ValueError as exc:
        raise AppError(SERVER_ERROR, "Unsupported Ucams playlist") from exc
    return Response(content=body, media_type="application/vnd.apple.mpegurl", headers=_NO_CACHE_HEADERS)


@router.get("/{id}/hls/archive/resource/{key}")
async def get_ufanet_archive_resource(
    id: int, key: str, request: Request, start: int = Query(..., ge=1),
    duration: int = Query(..., ge=1, le=86400),
    payload=Depends(require_access_qs), db: AsyncSession = Depends(get_db),
):
    if not settings.HLS_ENABLED:
        raise AppError(BAD_REQUEST, "HLS стриминг отключен")
    cam = await _get_visible_cam(id, payload, db)
    if cam.connection_resource_id is None:
        raise AppError(FILE_NOT_FOUND, "Resource not found")
    await HLS_BUDGET.charge(request, 1)
    await ufanet_media.check_camera(db, cam)
    try:
        url, host = ufanet_media.open_resource(key, id, f'archive:{start}:{duration}')
        data, content_type = await ufanet_media.fetch(url, host, 32 * 1024 * 1024)
    except AppError:
        raise
    except Exception as exc:
        raise AppError(SERVER_ERROR, "Ucams archive resource unavailable") from exc
    return Response(content=data, media_type=content_type.split(';', 1)[0] or 'application/octet-stream', headers=_NO_CACHE_HEADERS)


@router.get("/{id}/hls/{segment}")
@supports("streaming")
async def get_hls_segment(
    id: int,
    segment: str,
    request: Request,
    ctx: AccessContext | None = Depends(_hls_access),
    db: AsyncSession = Depends(get_db),
):
    if not settings.HLS_ENABLED:
        raise AppError(BAD_REQUEST, "HLS стриминг отключен")

    # Validate segment filename to prevent path traversal
    if not segment.endswith(".ts") or "/" in segment or "\\" in segment or segment in (".", ".."):
        raise AppError(FILE_NOT_FOUND, "Segment not found")

    cam = await _cam_for_hls(id, ctx, db)
    if cam.connection_resource_id is not None:
        if ctx is None:
            ctx = await resolve_stream_access(id, request, request.headers.get('authorization'), request.query_params.get('token'), db)
            cam = await _get_cam_for_access(id, ctx, db)
        await HLS_BUDGET.charge(request, 1)
        if not segment.startswith('u-'):
            raise AppError(FILE_NOT_FOUND, "Segment not found")
        await ufanet_media.check_camera(db, cam)
        try:
            url, host = ufanet_media.open_resource(segment[2:-3], id, 'live')
            data, content_type = await ufanet_media.fetch(url, host, 32 * 1024 * 1024)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(SERVER_ERROR, "Ucams segment unavailable") from exc
        return Response(content=data, media_type=content_type.split(';', 1)[0] or 'application/octet-stream', headers=_NO_CACHE_HEADERS)
    if ctx is not None:
        await HLS_BUDGET.charge(request, 1)
        target = await view_proxy.view_target(db, cam.id)
        if target is not None:
            return await view_proxy.proxy_http(request, target)

    if not worker_enabled(settings, "hls"):
        raise AppError(BAD_REQUEST, "HLS стриминг отключен на этой ноде")

    try:
        session = await hls_manager.get_or_create_session(cam)
    except hls_manager.HLSStreamError as exc:
        raise AppError(SERVER_ERROR, str(exc))

    # Touch session; re-arm the idle stop (get_or_create_session cancelled it), so a
    # session started by a segment request can never outlive its viewer
    session.touch()
    hls_manager.schedule_idle_stop(cam.id)

    segment_path = session.temp_dir / segment
    if not segment_path.exists() or not segment_path.is_file():
        raise AppError(FILE_NOT_FOUND, "Segment not found")

    return FileResponse(
        segment_path,
        media_type="video/mp2t",
        headers=_LONG_CACHE_HEADERS,
    )


# --- WebRTC streaming ---------------------------------------------------------
