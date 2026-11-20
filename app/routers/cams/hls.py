from urllib.parse import quote

from fastapi import Depends, Header, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import BAD_REQUEST, FILE_NOT_FOUND, OBJECT_NOT_FOUND, SERVER_ERROR
from app.core.config import settings
from app.core.errors import AppError
from app.core.introspection import supports
from app.core.rate_limit import PointBudget
from app.db.session import get_db
from app.deps.auth import AccessContext, resolve_stream_access
from app.models.cams import Cam
from app.services import hls_manager
from app.services.cluster import view_proxy

from app.routers.cams._common import _LONG_CACHE_HEADERS, _NO_CACHE_HEADERS, _get_cam_for_access, router



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
    if ctx is not None:
        await HLS_BUDGET.charge(request, 1)
        target = await view_proxy.view_target(db, cam.id)
        if target is not None:
            return await view_proxy.proxy_http(request, target)

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
    if ctx is not None:
        await HLS_BUDGET.charge(request, 1)
        target = await view_proxy.view_target(db, cam.id)
        if target is not None:
            return await view_proxy.proxy_http(request, target)

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
