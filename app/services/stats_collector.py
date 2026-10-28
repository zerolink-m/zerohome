from __future__ import annotations

import asyncio
import logging
import os
import psutil
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.cams import Cam
from app.models.motion_event import MotionEvent
from app.models.user import User
from app.models.session import Session as UserSession
from app.models.sharing import Sharing

logger = logging.getLogger(__name__)


@dataclass
class StreamStats:
    active_hls: int = 0
    active_webrtc: int = 0
    active_legacy: int = 0
    total_clients: int = 0
    traffic_by_cam: Dict[int, dict] = field(default_factory=dict)


@dataclass
class ErrorStats:
    errors_by_type: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    http_4xx: int = 0
    http_5xx: int = 0
    rtsp_failures: int = 0
    onvif_failures: int = 0


# Global collectors
_stream_stats = StreamStats()
_error_stats = ErrorStats()
_request_times: Dict[str, List[float]] = defaultdict(list)
_snapshot_times: Dict[str, List[float]] = defaultdict(lambda: {"onvif": [], "rtsp": []})
_start_time = time.time()


def record_stream_active(stream_type: str, cam_id: int) -> None:
    """Record active stream session"""
    if stream_type == "hls":
        _stream_stats.active_hls += 1
    elif stream_type == "webrtc":
        _stream_stats.active_webrtc += 1
    elif stream_type == "legacy":
        _stream_stats.active_legacy += 1
    _stream_stats.total_clients += 1


def record_stream_inactive(stream_type: str, cam_id: int) -> None:
    """Record stream session end"""
    if stream_type == "hls" and _stream_stats.active_hls > 0:
        _stream_stats.active_hls -= 1
    elif stream_type == "webrtc" and _stream_stats.active_webrtc > 0:
        _stream_stats.active_webrtc -= 1
    elif stream_type == "legacy" and _stream_stats.active_legacy > 0:
        _stream_stats.active_legacy -= 1
    if _stream_stats.total_clients > 0:
        _stream_stats.total_clients -= 1


def record_error(error_type: str, status_code: int | None = None) -> None:
    """Record error occurrence"""
    _error_stats.errors_by_type[error_type] += 1
    if status_code:
        if 400 <= status_code < 500:
            _error_stats.http_4xx += 1
        elif 500 <= status_code < 600:
            _error_stats.http_5xx += 1


def record_rtsp_failure() -> None:
    """Record RTSP connection failure"""
    _error_stats.rtsp_failures += 1


def record_onvif_failure() -> None:
    """Record ONVIF operation failure"""
    _error_stats.onvif_failures += 1


def record_request_time(endpoint: str, duration: float) -> None:
    """Record request duration for endpoint"""
    times = _request_times[endpoint]
    times.append(duration)
    # Keep only last 1000 samples per endpoint
    if len(times) > 1000:
        times.pop(0)


def record_snapshot_time(method: str, duration: float) -> None:
    """Record snapshot generation time (onvif or rtsp)"""
    times = _snapshot_times["global"][method]
    times.append(duration)
    if len(times) > 1000:
        times.pop(0)


def get_system_stats() -> dict:
    """Get system resource usage"""
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    net_io = psutil.net_io_counters()

    return {
        "cpu_percent": round(cpu_percent, 2),
        "memory": {
            "used_mb": round(memory.used / (1024 * 1024), 2),
            "available_mb": round(memory.available / (1024 * 1024), 2),
            "total_mb": round(memory.total / (1024 * 1024), 2),
            "percent": round(memory.percent, 2),
        },
        "disk": {
            "used_gb": round(disk.used / (1024 ** 3), 2),
            "free_gb": round(disk.free / (1024 ** 3), 2),
            "total_gb": round(disk.total / (1024 ** 3), 2),
            "percent": round(disk.percent, 2),
        },
        "network": {
            "bytes_sent": net_io.bytes_sent,
            "bytes_recv": net_io.bytes_recv,
            "packets_sent": net_io.packets_sent,
            "packets_recv": net_io.packets_recv,
        },
        "uptime_seconds": int(time.time() - _start_time),
    }


def get_stream_stats() -> dict:
    """Get streaming statistics"""
    return {
        "active_streams": {
            "hls": _stream_stats.active_hls,
            "webrtc": _stream_stats.active_webrtc,
            "legacy": _stream_stats.active_legacy,
            "total": _stream_stats.active_hls + _stream_stats.active_webrtc + _stream_stats.active_legacy,
        },
        "total_clients": _stream_stats.total_clients,
    }


def get_error_stats() -> dict:
    """Get error statistics"""
    top_errors = sorted(_error_stats.errors_by_type.items(), key=lambda x: x[1], reverse=True)[:10]
    return {
        "errors_by_type": dict(top_errors),
        "http_4xx": _error_stats.http_4xx,
        "http_5xx": _error_stats.http_5xx,
        "rtsp_failures": _error_stats.rtsp_failures,
        "onvif_failures": _error_stats.onvif_failures,
    }


def get_performance_stats() -> dict:
    """Get performance metrics"""
    endpoint_stats = {}
    for endpoint, times in _request_times.items():
        if times:
            endpoint_stats[endpoint] = {
                "count": len(times),
                "avg_ms": round(sum(times) / len(times) * 1000, 2),
                "min_ms": round(min(times) * 1000, 2),
                "max_ms": round(max(times) * 1000, 2),
            }

    snapshot_stats = {}
    for method in ["onvif", "rtsp"]:
        times = _snapshot_times["global"][method]
        if times:
            snapshot_stats[method] = {
                "count": len(times),
                "avg_ms": round(sum(times) / len(times) * 1000, 2),
                "min_ms": round(min(times) * 1000, 2),
                "max_ms": round(max(times) * 1000, 2),
            }

    return {
        "endpoints": endpoint_stats,
        "snapshots": snapshot_stats,
    }


async def get_camera_stats(db: AsyncSession) -> dict:
    """Get camera statistics from database"""
    total_cams = await db.scalar(select(func.count(Cam.id)))
    enabled_cams = await db.scalar(select(func.count(Cam.id)).where(Cam.enabled == 1))
    onvif_cams = await db.scalar(select(func.count(Cam.id)).where(Cam.onvif_enabled == 1))
    ptz_cams = await db.scalar(select(func.count(Cam.id)).where(Cam.onvif_ptz_enabled == 1))
    recording_cams = await db.scalar(select(func.count(Cam.id)).where(Cam.record == 1))

    return {
        "total": total_cams or 0,
        "enabled": enabled_cams or 0,
        "disabled": (total_cams or 0) - (enabled_cams or 0),
        "onvif_enabled": onvif_cams or 0,
        "ptz_enabled": ptz_cams or 0,
        "recording_enabled": recording_cams or 0,
    }


async def get_recording_stats(db: AsyncSession) -> dict:
    """Get recording statistics"""
    # Calculate total recordings disk usage
    total_size = 0
    recording_count = 0

    result = await db.execute(select(Cam).where(Cam.record == 1))
    cams = result.scalars().all()

    for cam in cams:
        if cam.record_meta and "local" in cam.record_meta:
            path = cam.record_meta["local"].get("path")
            if path and os.path.exists(path):
                try:
                    for root, dirs, files in os.walk(path):
                        for file in files:
                            if file.endswith((".mp4", ".mkv", ".avi")):
                                recording_count += 1
                                file_path = os.path.join(root, file)
                                total_size += os.path.getsize(file_path)
                except Exception:
                    pass

    return {
        "total_recordings": recording_count,
        "total_size_gb": round(total_size / (1024 ** 3), 2),
        "total_size_bytes": total_size,
    }


async def get_motion_stats(db: AsyncSession, period_hours: int = 24) -> dict:
    """Get motion detection statistics"""
    cutoff_time = int(time.time()) - (period_hours * 3600)

    # Total events in period
    total_events = await db.scalar(
        select(func.count(MotionEvent.id)).where(MotionEvent.detected_at >= cutoff_time)
    )

    # Events by object type
    events_by_type_query = await db.execute(
        select(MotionEvent.object_type, func.count(MotionEvent.id).label("count"))
        .where(MotionEvent.detected_at >= cutoff_time)
        .group_by(MotionEvent.object_type)
    )
    events_by_type = {row[0]: row[1] for row in events_by_type_query}

    # Average confidence
    avg_confidence_val = await db.scalar(
        select(func.avg(MotionEvent.confidence)).where(MotionEvent.detected_at >= cutoff_time)
    )
    avg_confidence = round(float(avg_confidence_val), 2) if avg_confidence_val else 0.0

    # License plates detected
    ocr_count = await db.scalar(
        select(func.count(MotionEvent.id))
        .where(MotionEvent.detected_at >= cutoff_time)
        .where(MotionEvent.license_plate.isnot(None))
    )

    # Top cameras by events
    top_cams_query = await db.execute(
        select(MotionEvent.cam_id, func.count(MotionEvent.id).label("count"))
        .where(MotionEvent.detected_at >= cutoff_time)
        .group_by(MotionEvent.cam_id)
        .order_by(func.count(MotionEvent.id).desc())
        .limit(10)
    )
    top_cams = [{"cam_id": row[0], "events": row[1]} for row in top_cams_query]

    return {
        "period_hours": period_hours,
        "total_events": total_events or 0,
        "events_by_object": events_by_type,
        "avg_confidence": avg_confidence,
        "license_plates_detected": ocr_count or 0,
        "top_cameras": top_cams,
    }


async def get_user_stats(db: AsyncSession) -> dict:
    """Get user statistics"""
    total_users = await db.scalar(select(func.count(User.id)))

    # Active sessions (sessions updated in last 30 days)
    now = int(time.time())
    cutoff = now - (30 * 24 * 3600)  # 30 days
    active_sessions = await db.scalar(
        select(func.count(UserSession.id)).where(UserSession.updated_at > cutoff)
    )

    # Active shares
    active_shares = await db.scalar(select(func.count(Sharing.id)))

    return {
        "total_users": total_users or 0,
        "active_sessions": active_sessions or 0,
        "active_shares": active_shares or 0,
    }


async def get_all_stats(period_hours: int = 24) -> dict:
    """Get all statistics combined"""
    async with AsyncSessionLocal() as db:
        return {
            "system": get_system_stats(),
            "streams": get_stream_stats(),
            "cameras": await get_camera_stats(db),
            "recordings": await get_recording_stats(db),
            "motion": await get_motion_stats(db, period_hours),
            "users": await get_user_stats(db),
            "errors": get_error_stats(),
            "performance": get_performance_stats(),
        }
