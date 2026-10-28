"""Persistent camera ingest (stage 1: infrastructure only).

    from app.services.ingest import get_hub
    async for chunk in get_hub().subscribe(cam_id): ...
"""
from app.services.ingest.hub import CamHub, IngestHub, Subscription
from app.services.ingest.lifecycle import (
    get_supervisor,
    on_cam_deleted,
    on_cam_saved,
    start_all_enabled,
    stop_all,
)
from app.services.ingest.supervisor import IngestSupervisor, IngestTarget


def get_hub() -> IngestHub:
    """The process-wide hub (the one the app's supervisor feeds)."""
    return get_supervisor().hub


__all__ = [
    "CamHub", "IngestHub", "IngestSupervisor", "IngestTarget", "Subscription",
    "get_hub", "get_supervisor", "on_cam_deleted", "on_cam_saved",
    "start_all_enabled", "stop_all",
]
