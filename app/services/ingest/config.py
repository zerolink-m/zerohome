"""Ingest tunables, snapshotted from `settings` (tests build their own)."""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings


@dataclass
class IngestConfig:
    stall_seconds: float = 10.0
    restart_min_minutes: float = 30.0
    restart_max_minutes: float = 60.0
    backoff_max_seconds: float = 30.0
    backoff_initial_seconds: float = 1.0
    # a run that produced data for this long counts as stable: backoff resets
    stable_seconds: float = 30.0
    # while viewers block a planned restart, look again after this long
    restart_recheck_seconds: float = 60.0
    monitor_tick_seconds: float = 0.5
    term_timeout_seconds: float = 3.0
    kill_timeout_seconds: float = 2.0
    stop_timeout_seconds: float = 8.0
    rtsp_timeout_seconds: float = 10.0
    registry_path: str = ""

    @classmethod
    def from_settings(cls) -> "IngestConfig":
        return cls(
            stall_seconds=settings.INGEST_STALL_SECONDS,
            restart_min_minutes=settings.INGEST_RESTART_MIN_MINUTES,
            restart_max_minutes=settings.INGEST_RESTART_MAX_MINUTES,
            backoff_max_seconds=settings.INGEST_BACKOFF_MAX_SECONDS,
            stop_timeout_seconds=settings.INGEST_STOP_TIMEOUT_SECONDS,
            registry_path=settings.ingest_registry_path,
        )
