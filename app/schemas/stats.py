from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel


class SystemStats(BaseModel):
    cpu_percent: float
    memory: Dict[str, float]
    disk: Dict[str, float]
    network: Dict[str, int]
    uptime_seconds: int


class StreamStats(BaseModel):
    active_streams: Dict[str, int]
    total_clients: int


class IngestCamStats(BaseModel):
    running: bool
    state: str
    last_error: Optional[str] = None
    restarts: int
    seconds_since_data: Optional[float] = None
    uptime_seconds: Optional[float] = None
    consumers: int
    has_init: bool
    has_gop: bool


class CameraStats(BaseModel):
    total: int
    enabled: int
    disabled: int
    onvif_enabled: int
    ptz_enabled: int
    recording_enabled: int
    # per-camera persistent ingest hub state keyed by cam id; null when ingest is disabled
    ingest: Optional[Dict[str, IngestCamStats]] = None


class RecordingStats(BaseModel):
    total_recordings: int
    total_size_gb: float
    total_size_bytes: int


class MotionStats(BaseModel):
    period_hours: int
    total_events: int
    events_by_object: Dict[str, int]
    avg_confidence: float
    license_plates_detected: int
    top_cameras: List[Dict[str, int]]


class UserStats(BaseModel):
    total_users: int
    active_sessions: int
    active_shares: int


class ErrorStats(BaseModel):
    errors_by_type: Dict[str, int]
    http_4xx: int
    http_5xx: int
    rtsp_failures: int
    onvif_failures: int


class PerformanceStats(BaseModel):
    endpoints: Dict[str, Dict[str, float]]
    snapshots: Dict[str, Dict[str, float]]


class AllStats(BaseModel):
    system: SystemStats
    streams: StreamStats
    cameras: CameraStats
    recordings: RecordingStats
    motion: MotionStats
    users: UserStats
    errors: ErrorStats
    performance: PerformanceStats
