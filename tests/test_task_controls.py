"""Background switches must not turn off unrelated cleanup or bypass the motion gate."""
import importlib
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import Settings, settings
from app.models.user import User
from app.models.push_settings import PushSettings
from app.services import motion_lifecycle
from app.services.motion_detection import manager
from tests.conftest import bearer, mint_test_token

main = importlib.import_module("app.main")
WORKERS = [
    ("SESSION_CLEANUP_ENABLED", main.auth, "start_session_cleanup"),
    ("HEALTHCHECK_ENABLED", main.healthcheck, "start"),
    ("SWITCH_SYNC_ENABLED", main.switch_sync, "start"),
    ("CONNECTION_SYNC_ENABLED", main.connection_sync, "start"),
    ("TUYA_EVENTS_ENABLED", main.tuya_events, "start"),
    ("MOTION_CLEANUP_ENABLED", main.motion_cleanup, "start"),
]


def test_task_defaults_preserve_behavior():
    for flag, _, _ in WORKERS:
        assert Settings.model_fields[flag].default is True
    assert Settings.model_fields["MOTION_DETECTION_ENABLED"].default is True
    for flag in ("HLS_ENABLED", "WEBRTC_ENABLED", "INGEST_ENABLED"):
        assert Settings.model_fields[flag].default is False


@pytest.mark.parametrize("disabled", [entry[0] for entry in WORKERS])
async def test_each_worker_can_be_disabled_independently(monkeypatch, disabled):
    mocks = {}
    for flag, module, name in WORKERS:
        monkeypatch.setattr(settings, flag, flag != disabled)
        mocks[flag] = Mock()
        monkeypatch.setattr(module, name, mocks[flag])
    rate_cleanup = Mock()
    monkeypatch.setattr(main, "start_cleanup_task", rate_cleanup)
    monkeypatch.setattr(settings, "HLS_ENABLED", False)
    monkeypatch.setattr(settings, "WEBRTC_ENABLED", False)
    for module, names in [
        (main.ingest, ("start_all_enabled", "stop_all")),
        (main.stream_manager, ("startup", "stop_all")),
        (main.hls_manager, ("startup", "stop_all")),
        (main.webrtc_manager, ("startup",)),
        (main.snapshot, ("startup", "stop_all")),
        (main.motion_detection, ("startup",)),
        (main.motion_lifecycle, ("start_all_enabled", "stop_all")),
        (main.tuya_events, ("stop_all",)),
        (main.tuya_client_pool, ("close_all",)),
        (main.motion_cleanup, ("stop",)),
        (main.auth, ("stop_session_cleanup",)),
    ]:
        for name in names:
            monkeypatch.setattr(module, name, AsyncMock())
    async with main.lifespan(main.app):
        pass
    rate_cleanup.assert_called_once()
    for flag, mock in mocks.items():
        assert mock.call_count == (0 if flag == disabled else 1)


async def test_disabled_motion_startup_never_queries_database(monkeypatch):
    monkeypatch.setattr(settings, "MOTION_DETECTION_ENABLED", False)
    db = Mock(side_effect=AssertionError("Motion startup touched DB"))
    monkeypatch.setattr(motion_lifecycle, "AsyncSessionLocal", db)
    await motion_lifecycle.start_all_enabled()
    db.assert_not_called()


async def test_disabled_motion_runtime_gate_precedes_database_and_model(monkeypatch):
    monkeypatch.setattr(settings, "MOTION_DETECTION_ENABLED", False)
    db = Mock(side_effect=AssertionError("Motion start touched DB"))
    model = Mock(side_effect=AssertionError("Motion session was created"))
    monkeypatch.setattr(manager, "AsyncSessionLocal", db)
    monkeypatch.setattr(manager, "MotionDetectionSession", model)
    assert await manager.start_motion_detection(123) is False
    db.assert_not_called()
    model.assert_not_called()


async def test_global_motion_gate_survives_api_settings_changes(
    client, mk_user, mk_cam, sessionmaker_, monkeypatch,
):
    monkeypatch.setattr(settings, "MOTION_DETECTION_ENABLED", False)
    owner = await mk_user("motion-gate@example.com")
    cam = await mk_cam(owner)
    async with sessionmaker_() as db:
        (await db.get(User, owner)).motion_detection_allowed = 1
        # Explicit id keeps this focused test independent of SQLite's handling
        # of the production BIGINT autoincrement type.
        db.add(PushSettings(id=1, user_id=owner, device_type="cams", device_id=cam,
                            created_at=1, updated_at=1))
        await db.commit()
    headers = bearer(await mint_test_token(owner))
    # User preferences remain editable, but cannot override the process-wide gate.
    response = await client.patch(f"/v1/cams/{cam}/push-settings", headers=headers,
        json={"data": {"push_settings": {"motion_detection_enabled": 1}}})
    assert response.status_code == 200
    db_factory = Mock(side_effect=AssertionError("Motion manager touched DB"))
    monkeypatch.setattr(manager, "AsyncSessionLocal", db_factory)
    response = await client.post(f"/v1/cams/{cam}/motion/start", headers=headers)
    assert response.json()["data"]["motion"] == {
        "cam_id": cam, "started": False, "is_active": False,
    }
    db_factory.assert_not_called()
