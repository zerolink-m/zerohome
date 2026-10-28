from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.response import success
from app.core.codes import FORBIDDEN_ROLE
from app.core.rate_limit import rate_limit
from app.db.session import get_db
from app.deps.auth import require_access
from app.core.security import TokenPayload
from app.schemas.stats import (
    AllStats,
    CameraStats,
    ErrorStats,
    MotionStats,
    PerformanceStats,
    RecordingStats,
    StreamStats,
    SystemStats,
    UserStats,
)
from app.services import stats_collector

router = APIRouter(prefix="/v1/stats", tags=["stats"])


def _require_admin(payload: TokenPayload = Depends(require_access)) -> TokenPayload:
    """Require admin role for stats access"""
    if payload.role != "admin":
        raise AppError(FORBIDDEN_ROLE, "Доступ только для администраторов")
    return payload


@router.get("", dependencies=[Depends(rate_limit("stats", capacity=20, per_minute=20))])
async def get_all_stats(
    period: int = Query(24, ge=1, le=168, description="Period in hours for motion stats (1-168)"),
    payload: TokenPayload = Depends(_require_admin),
) -> dict:
    """Get all application statistics (admin only)"""
    stats = await stats_collector.get_all_stats(period_hours=period)
    return success(AllStats(**stats).model_dump())


@router.get("/system", dependencies=[Depends(rate_limit("stats", capacity=20, per_minute=20))])
async def get_system_stats(
    payload: TokenPayload = Depends(_require_admin),
) -> dict:
    """Get system resource statistics (admin only)"""
    stats = stats_collector.get_system_stats()
    return success({"system": SystemStats(**stats).model_dump()})


@router.get("/streams", dependencies=[Depends(rate_limit("stats", capacity=20, per_minute=20))])
async def get_stream_stats(
    payload: TokenPayload = Depends(_require_admin),
) -> dict:
    """Get streaming statistics (admin only)"""
    stats = stats_collector.get_stream_stats()
    return success({"streams": StreamStats(**stats).model_dump()})


@router.get("/cameras", dependencies=[Depends(rate_limit("stats", capacity=20, per_minute=20))])
async def get_camera_stats(
    payload: TokenPayload = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get camera statistics (admin only)"""
    stats = await stats_collector.get_camera_stats(db)
    return success({"cameras": CameraStats(**stats).model_dump()})


@router.get("/recordings", dependencies=[Depends(rate_limit("stats", capacity=20, per_minute=20))])
async def get_recording_stats(
    payload: TokenPayload = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get recording statistics (admin only)"""
    stats = await stats_collector.get_recording_stats(db)
    return success({"recordings": RecordingStats(**stats).model_dump()})


@router.get("/motion", dependencies=[Depends(rate_limit("stats", capacity=20, per_minute=20))])
async def get_motion_stats(
    period: int = Query(24, ge=1, le=168, description="Period in hours (1-168)"),
    payload: TokenPayload = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get motion detection statistics (admin only)"""
    stats = await stats_collector.get_motion_stats(db, period_hours=period)
    return success({"motion": MotionStats(**stats).model_dump()})


@router.get("/users", dependencies=[Depends(rate_limit("stats", capacity=20, per_minute=20))])
async def get_user_stats(
    payload: TokenPayload = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get user statistics (admin only)"""
    stats = await stats_collector.get_user_stats(db)
    return success({"users": UserStats(**stats).model_dump()})


@router.get("/errors", dependencies=[Depends(rate_limit("stats", capacity=20, per_minute=20))])
async def get_error_stats(
    payload: TokenPayload = Depends(_require_admin),
) -> dict:
    """Get error statistics (admin only)"""
    stats = stats_collector.get_error_stats()
    return success({"errors": ErrorStats(**stats).model_dump()})


@router.get("/performance", dependencies=[Depends(rate_limit("stats", capacity=20, per_minute=20))])
async def get_performance_stats(
    payload: TokenPayload = Depends(_require_admin),
) -> dict:
    """Get performance metrics (admin only)"""
    stats = stats_collector.get_performance_stats()
    return success({"performance": PerformanceStats(**stats).model_dump()})
