import time

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles

from app.core.security import create_access_token
from app.models.motion_event import MotionEvent
from tests.conftest import bearer


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(type_, compiler, **kw):
    # SQLite only autoincrements INTEGER PRIMARY KEY (push_settings / motion_events use BigInteger ids)
    return "INTEGER"


def tok(uid, role="user"):
    return create_access_token(uid, 1, role)


@pytest.fixture
async def setup(mk_user, mk_cam):
    owner = await mk_user("owner@x.io")
    other = await mk_user("other@x.io")
    admin = await mk_user("adm@x.io", role="admin")
    cam = await mk_cam(owner)
    return owner, other, admin, cam


# ---------------- push settings ----------------
async def test_push_settings_get_and_patch(client, setup):
    owner, other, _, cam = setup
    url = f"/v1/cams/{cam}/push-settings"
    r = await client.get(url, headers=bearer(tok(owner)))
    assert r.status_code == 200 and r.json()["code"] == "000"
    ps = r.json()["data"]["push_settings"]
    assert ps["device_id"] == cam and ps["enabled"] == 1

    body = {"data": {"push_settings": {"notify_offline": 0}}}
    r = await client.patch(url, headers=bearer(tok(owner)), json=body)
    assert r.json()["code"] == "003"
    assert r.json()["data"]["push_settings"]["notify_offline"] == 0
    # same values again: not changed
    r = await client.patch(url, headers=bearer(tok(owner)), json=body)
    assert r.json()["code"] == "005"


async def test_push_settings_validation_and_access(client, setup):
    owner, other, _, cam = setup
    url = f"/v1/cams/{cam}/push-settings"
    r = await client.patch(url, headers=bearer(tok(owner)), json={"enabled": 0})
    assert r.json()["code"] == "109"
    r = await client.patch(url, headers=bearer(tok(owner)),
                           json={"data": {"push_settings": {"enabled": 5}}})
    assert r.json()["code"] == "109" and "enabled" in r.json()["error"]["fields"]
    r = await client.patch(url, headers=bearer(tok(owner)),
                           json={"data": {"push_settings": {"bogus": 1}}})
    assert r.json()["code"] == "109"
    assert (await client.get(url, headers=bearer(tok(other)))).json()["code"] == "051"
    assert (await client.get("/v1/cams/9999/push-settings", headers=bearer(tok(owner)))).json()["code"] == "082"


# ---------------- motion ----------------
@pytest.fixture
async def events(sessionmaker_, setup):
    _, _, _, cam = setup
    now = int(time.time())
    async with sessionmaker_() as s:
        for i, t in enumerate(["person", "car", "person"]):
            s.add(MotionEvent(cam_id=cam, detected_at=now + i, object_type=t, confidence=0.9,
                              object_size=0.1, photo_filename=f"{i}.jpg"))
        await s.commit()
    return cam


async def test_motion_events_list_get_delete(client, setup, events):
    owner, other, _, cam = setup
    h = bearer(tok(owner))
    r = await client.get(f"/v1/cams/{cam}/motion/events", headers=h)
    d = r.json()["data"]
    assert r.json()["code"] == "000" and d["settings"]["total"] == 3
    times = [e["detected_at"] for e in d["motion_events"]]
    assert times == sorted(times, reverse=True)  # newest first by default

    r = await client.get(f"/v1/cams/{cam}/motion/events?search=person&search_column=object_type&search_method=equals", headers=h)
    assert r.json()["data"]["settings"]["total"] == 2

    eid = d["motion_events"][0]["id"]
    r = await client.get(f"/v1/motion/events/{eid}", headers=h)
    assert r.json()["data"]["motion_event"]["id"] == eid
    assert (await client.get(f"/v1/motion/events/{eid}", headers=bearer(tok(other)))).json()["code"] == "051"

    assert (await client.delete(f"/v1/motion/events/{eid}", headers=bearer(tok(other)))).json()["code"] == "051"
    r = await client.delete(f"/v1/motion/events/{eid}", headers=h)
    assert r.json()["code"] == "007" and r.json()["data"]["motion_event"] == {"id": eid}
    assert (await client.get(f"/v1/motion/events/{eid}", headers=h)).json()["code"] == "082"


async def test_motion_events_empty_and_bad_limit(client, setup):
    owner, _, _, cam = setup
    r = await client.get(f"/v1/cams/{cam}/motion/events", headers=bearer(tok(owner)))
    assert r.json()["code"] == "012" and r.json()["data"]["motion_events"] == []
    r = await client.get(f"/v1/cams/{cam}/motion/events?limit=500", headers=bearer(tok(owner)))
    assert r.json()["code"] == "183"


async def test_motion_control(client, setup, monkeypatch, sessionmaker_):
    from app.models.user import User
    from app.services import motion_detection
    owner, other, _, cam = setup
    state = {"on": False}

    async def start(cid):
        state["on"] = True
        return True

    async def stop(cid):
        state["on"] = False
        return True

    monkeypatch.setattr(motion_detection, "start_motion_detection", start)
    monkeypatch.setattr(motion_detection, "stop_motion_detection", stop)
    monkeypatch.setattr(motion_detection, "is_motion_detection_active", lambda cid: state["on"])
    h = bearer(tok(owner))

    # owner not allowed motion detection yet
    assert (await client.post(f"/v1/cams/{cam}/motion/start", headers=h)).json()["code"] == "051"
    async with sessionmaker_() as s:
        (await s.get(User, owner)).motion_detection_allowed = 1
        await s.commit()
    r = await client.post(f"/v1/cams/{cam}/motion/start", headers=h)
    assert r.json()["code"] == "000" and r.json()["data"]["motion"] == {"cam_id": cam, "started": True, "is_active": True}
    r = await client.get(f"/v1/cams/{cam}/motion/status", headers=h)
    assert r.json()["data"]["motion"]["is_active"] is True
    r = await client.post(f"/v1/cams/{cam}/motion/stop", headers=h)
    assert r.json()["data"]["motion"]["is_active"] is False
    assert (await client.post(f"/v1/cams/{cam}/motion/stop", headers=bearer(tok(other)))).json()["code"] == "051"


# ---------------- stats ----------------
async def test_stats_admin_only_and_shape(client, setup, monkeypatch):
    from app.services import stats_collector
    owner, _, admin, _ = setup
    monkeypatch.setattr(stats_collector, "get_stream_stats", lambda: {"active_streams": {"fmp4": 1}, "total_clients": 2})
    monkeypatch.setattr(stats_collector, "get_error_stats", lambda: {
        "errors_by_type": {}, "http_4xx": 1, "http_5xx": 0, "rtsp_failures": 0, "onvif_failures": 0})
    assert (await client.get("/v1/stats/streams", headers=bearer(tok(owner)))).json()["code"] == "050"
    r = await client.get("/v1/stats/streams", headers=bearer(tok(admin, "admin")))
    assert r.json()["code"] == "000"
    assert r.json()["data"] == {"streams": {"active_streams": {"fmp4": 1}, "total_clients": 2}}
    r = await client.get("/v1/stats/errors", headers=bearer(tok(admin, "admin")))
    assert r.json()["data"]["errors"]["http_4xx"] == 1


async def test_stats_all_and_bad_period(client, setup, monkeypatch):
    from app.services import stats_collector
    _, _, admin, _ = setup
    h = bearer(tok(admin, "admin"))
    full = {
        "system": {"cpu_percent": 1.0, "memory": {}, "disk": {}, "network": {}, "uptime_seconds": 1},
        "streams": {"active_streams": {}, "total_clients": 0},
        "cameras": {"total": 0, "enabled": 0, "disabled": 0, "onvif_enabled": 0, "ptz_enabled": 0, "recording_enabled": 0},
        "recordings": {"total_recordings": 0, "total_size_gb": 0.0, "total_size_bytes": 0},
        "motion": {"period_hours": 24, "total_events": 0, "events_by_object": {}, "avg_confidence": 0.0,
                   "license_plates_detected": 0, "top_cameras": []},
        "users": {"total_users": 0, "active_sessions": 0, "active_shares": 0},
        "errors": {"errors_by_type": {}, "http_4xx": 0, "http_5xx": 0, "rtsp_failures": 0, "onvif_failures": 0},
        "performance": {"endpoints": {}, "snapshots": {}},
    }

    async def fake_all(period_hours):
        return full

    monkeypatch.setattr(stats_collector, "get_all_stats", fake_all)
    r = await client.get("/v1/stats?period=12", headers=h)
    assert r.json()["code"] == "000" and set(r.json()["data"]) == set(full)
    r = await client.get("/v1/stats?period=999", headers=h)
    assert r.status_code == 400 and "period" in r.json()["error"]["query"]


# ---------------- config ----------------
@pytest.fixture
def fv(tmp_path, monkeypatch):
    import json
    from app.services import frontend_version as f

    def _set(versions):
        path = tmp_path / "frontend_versions.json"
        path.write_text(json.dumps({"versions": versions}))
        monkeypatch.setattr(f, "_JSON_PATH", path)
        f.reset_cache_for_tests()

    yield _set
    f.reset_cache_for_tests()


async def test_config_endpoints(client, fv):
    fv({"v*.*.*-*": 0})
    r = await client.get("/v1/config/frontend")
    assert r.json()["code"] == "000" and "version" in r.json()["data"]["config"]
    r = await client.get("/v1/config/frontend/supported_streams")
    streams = r.json()["data"]["streams"]
    assert [s["format"] for s in streams] == ["fmp4"]
    r = await client.get("/v1/health")
    assert r.json()["data"] == {"status": "ok"}
    r = await client.get("/v1/version")
    assert r.json()["data"]["version"]["api"] == "v1"


async def test_config_frontend_versions(client, fv):
    fv({"v*.*.*-*": 63 - 63 + 67, "v3.(0-4).*-*": 5, "v3.5.*-*": 0})
    r = await client.get("/v1/config?v=v3.5.1-Release")
    assert r.status_code == 200 and r.json()["code"] == "000"
    assert r.json()["data"]["config"]["version"] == "v3.5.1-Release"
    assert "actual_version" in r.json()["data"]["config"]
    # update available: data is returned
    r = await client.get("/v1/config?v=v3.2.0-Beta")
    assert r.status_code == 200 and r.json()["code"] == "020"
    assert r.json()["data"]["config"]["version"] == "v3.2.0-Beta"
    # update needed: no data
    r = await client.get("/v1/config?v=v1.0.0-Release")
    assert r.status_code == 403 and r.json()["code"] == "063" and r.json()["data"] == {}
    # missing v -> needed
    r = await client.get("/v1/config")
    assert r.status_code == 403 and r.json()["code"] == "063" and r.json()["data"] == {}
    # malformed v
    r = await client.get("/v1/config?v=1.2.3")
    assert r.status_code == 400 and r.json()["code"] == "040" and "v" in r.json()["error"]["query"]
    # no pattern matches -> needed
    fv({"v9.*.*-*": 0})
    r = await client.get("/v1/config?v=v3.5.1-Release")
    assert r.json()["code"] == "063"
    assert set(r.json()) == {"code", "code_detail", "error", "metadata", "data"}
