import pytest

from app.core.config import settings
from app.services.ingest import lifecycle
from app.services.ingest.config import IngestConfig
from app.services.ingest.hub import STATE_ONLINE, IngestHub
from app.services.ingest.supervisor import IngestSupervisor
from tests.conftest import bearer
from tests.ingest.conftest import alive, starts, wait_until
from tests.test_api_cams_extra import req, tok


@pytest.fixture
def ingest_on(monkeypatch, fake_bin, fake_log, cfg, sessionmaker_):
    """INGEST_ENABLED with a supervisor on the fake ffmpeg; cam path selects the mode."""
    monkeypatch.setattr(settings, "INGEST_ENABLED", True)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", sessionmaker_)
    lifecycle.reset_supervisor()
    sup = IngestSupervisor(hub=IngestHub(), config=cfg)
    monkeypatch.setattr(lifecycle, "_supervisor", sup)
    yield sup
    lifecycle.reset_supervisor()


async def cleanup(sup):
    await sup.stop_all(timeout=3.0)


async def make(sm, owner, enabled, path="ok", addr="127.0.0.1"):
    import time
    from app.models.cams import Cam
    now = int(time.time())
    async with sm() as s:
        c = Cam(ownuser=owner, name="c", enabled=enabled, proto="rtsp", addr=addr, port=554,
                path=path, username="u", password="pw", added_at=now, updated_at=now)
        s.add(c)
        await s.commit()
        return c.id


async def test_disabled_flag_starts_nothing(monkeypatch, fake_bin, fake_log, cfg, mk_user, sessionmaker_):
    monkeypatch.setattr(settings, "INGEST_ENABLED", False)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", sessionmaker_)
    lifecycle.reset_supervisor()
    owner = await mk_user("o@x.io")
    await make(sessionmaker_, owner, 1)
    await lifecycle.start_all_enabled()
    assert lifecycle._supervisor is None
    assert starts(fake_log) == []
    await lifecycle.on_cam_saved(type("C", (), {"id": 1})())
    await lifecycle.on_cam_deleted(1)
    await lifecycle.stop_all()
    assert lifecycle._supervisor is None and starts(fake_log) == []


async def test_start_all_enabled_only_enabled_cams(ingest_on, mk_user, sessionmaker_, fake_log):
    owner = await mk_user("o@x.io")
    c1 = await make(sessionmaker_, owner, 1)
    c2 = await make(sessionmaker_, owner, 0)
    c3 = await make(sessionmaker_, owner, 1)
    await lifecycle.start_all_enabled()
    try:
        assert ingest_on.is_running(c1) and ingest_on.is_running(c3)
        assert not ingest_on.is_running(c2)
        assert await wait_until(lambda: len(starts(fake_log)) == 2)
        assert await wait_until(lambda: all(
            ingest_on.hub.get(c).state == STATE_ONLINE for c in (c1, c3)))
    finally:
        pids = [p for p, _ in starts(fake_log)]
        await lifecycle.stop_all()
    assert not any(alive(p) for p in pids)
    assert not ingest_on.get_status()


async def test_crud_hooks_follow_enabled_and_connection_fields(ingest_on, client, mk_user, mk_cam, fake_log, sessionmaker_):
    owner = await mk_user("o@x.io")
    h = bearer(await tok(owner))
    try:
        # create enabled -> started
        r = await client.post("/v1/cams", headers=h, json=req("cam", {
            "name": "n", "enabled": 1, "protocol": "rtsp", "address": "127.0.0.1",
            "port": 554, "path": "ok", "username": "u", "password": "pw"}))
        cid = r.json()["data"]["cam"]["id"]
        assert ingest_on.is_running(cid)
        assert await wait_until(lambda: len(starts(fake_log)) == 1)
        pid1 = starts(fake_log)[0][0]

        # unrelated field -> untouched
        await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {"name": "x"}))
        assert len(starts(fake_log)) == 1 and alive(pid1)

        # same value -> untouched
        await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {"port": 554}))
        assert len(starts(fake_log)) == 1

        # address/path change -> restart
        r = await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {"path": "ok?rate=10"}))
        assert r.json()["code"] == "003"
        assert await wait_until(lambda: len(starts(fake_log)) == 2)
        assert await wait_until(lambda: not alive(pid1))
        pid2 = starts(fake_log)[1][0]

        # password change -> restart
        await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {"password": "new"}))
        assert await wait_until(lambda: len(starts(fake_log)) == 3)

        # enabled=0 -> stopped
        await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {"enabled": 0}))
        assert not ingest_on.is_running(cid)
        assert await wait_until(lambda: not any(alive(p) for p, _ in starts(fake_log)))

        # enabled=1 -> started again
        await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {"enabled": 1}))
        assert ingest_on.is_running(cid)

        # delete -> stopped
        r = await client.delete(f"/v1/cams/{cid}", headers=h)
        assert r.json()["code"] == "007"
        assert not ingest_on.is_running(cid)
        assert await wait_until(lambda: not any(alive(p) for p, _ in starts(fake_log)))
    finally:
        await cleanup(ingest_on)


async def test_bulk_hooks(ingest_on, client, mk_user, fake_log):
    owner = await mk_user("o@x.io")
    h = bearer(await tok(owner))
    try:
        item = {"enabled": 1, "protocol": "rtsp", "address": "127.0.0.1", "port": 554,
                "path": "ok", "username": "u", "password": "pw"}
        r = await client.post("/v1/cams/bulk", headers=h, json={"data": {"cams": [item, {**item, "enabled": 0}]}})
        ids = [c["id"] for c in r.json()["data"]["cams"]]
        assert [ingest_on.is_running(i) for i in ids] == [True, False]
        r = await client.patch(f"/v1/cams/bulk/{ids[0]},{ids[1]}", headers=h,
                               json={"data": {"cams": [{"enabled": 0}, {"enabled": 1}]}})
        assert [ingest_on.is_running(i) for i in ids] == [False, True]
        await client.delete(f"/v1/cams/bulk/{ids[0]},{ids[1]}", headers=h)
        assert not any(ingest_on.is_running(i) for i in ids)
    finally:
        await cleanup(ingest_on)


async def test_ingest_failure_does_not_break_api(ingest_on, client, mk_user, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(ingest_on, "apply", boom)
    monkeypatch.setattr(ingest_on, "stop", boom)
    owner = await mk_user("o@x.io")
    h = bearer(await tok(owner))
    r = await client.post("/v1/cams", headers=h, json=req("cam", {
        "enabled": 1, "protocol": "rtsp", "address": "127.0.0.1", "port": 554,
        "username": "u", "password": "pw"}))
    assert r.status_code == 201
    cid = r.json()["data"]["cam"]["id"]
    r = await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {"address": "127.0.0.2"}))
    assert r.json()["code"] == "003"
    r = await client.delete(f"/v1/cams/{cid}", headers=h)
    assert r.json()["code"] == "007"


async def test_start_all_survives_db_error(ingest_on, monkeypatch):
    class Broken:
        def __call__(self):
            raise RuntimeError("db down")
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", Broken())
    await lifecycle.start_all_enabled()  # must not raise


def test_defaults_are_safe():
    from app.core.config import Settings
    assert Settings.model_fields["INGEST_ENABLED"].default is False
    assert Settings.model_fields["INGEST_STALL_SECONDS"].default == 10
    assert Settings.model_fields["INGEST_IDLE_TRANSCODE_LINGER_SECONDS"].default == 45
    assert (Settings.model_fields["INGEST_RESTART_MIN_MINUTES"].default,
            Settings.model_fields["INGEST_RESTART_MAX_MINUTES"].default) == (30, 60)


def test_registry_path_default_under_cache_dir(monkeypatch):
    monkeypatch.setattr(settings, "INGEST_REGISTRY_DIR", "")
    assert settings.ingest_registry_path.endswith("/ingest/registry.json")
    monkeypatch.setattr(settings, "INGEST_REGISTRY_DIR", "/run/zc")
    assert settings.ingest_registry_path == "/run/zc/registry.json"
