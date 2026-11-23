"""Extra coverage: shares/invitations lists, push settings (switches), motion photo,
stats (admin only, DB-backed), OPTIONS on list endpoints."""
import time

import pytest

from app.core.security import decode_share_token
from app.models.motion_event import MotionEvent
from app.models.switch import Switch
from tests.conftest import bearer
from tests.test_api_push_motion_stats_config import tok  # noqa: F401  (also registers BigInteger/sqlite shim)


@pytest.fixture
async def setup(mk_user, mk_cam):
    owner = await mk_user("owner@x.io")
    other = await mk_user("other@x.io")
    admin = await mk_user("adm@x.io", role="admin")
    cam = await mk_cam(owner)
    return owner, other, admin, cam


# ---------------- shares / invitations lists ----------------
async def test_shares_list_settings_and_empty(client, setup):
    owner, other, admin, cam = setup
    h = bearer(await tok(owner))
    r = await client.get("/v1/shares", headers=h)
    assert r.json()["code"] == "019" and r.json()["data"]["shares"] == []
    assert r.json()["data"]["settings"]["total"] == 0
    for _ in range(3):
        body = {"data": {"share": {"device_type": "cams", "device_id": cam, "type": "stream"}}}
        c = await client.post("/v1/shares", headers=h, json=body)
        assert c.status_code == 201 and c.json()["code"] == "039"
        assert decode_share_token(c.json()["data"]["share"]["token"])
    r = await client.get("/v1/shares?limit=2&offset=0&sort_by=id&order=desc", headers=h)
    d = r.json()["data"]
    assert r.json()["code"] == "000" and d["settings"]["total"] == 3 and d["settings"]["limit"] == 2
    assert len(d["shares"]) == 2 and d["shares"][0]["id"] > d["shares"][1]["id"]
    assert "page" not in r.json()["metadata"]
    # other user sees none, admin sees all
    assert (await client.get("/v1/shares", headers=bearer(await tok(other)))).json()["code"] == "019"
    assert (await client.get("/v1/shares", headers=bearer(await tok(admin, "admin")))).json()["data"]["settings"]["total"] == 3
    # bad limit
    assert (await client.get("/v1/shares?limit=0", headers=h)).status_code == 400


async def test_invitations_list_settings_and_accept_by_admin(client, setup):
    owner, other, admin, cam = setup
    h = bearer(await tok(owner))
    r = await client.get("/v1/invitations", headers=h)
    assert r.json()["code"] == "019" and r.json()["data"]["invitations"] == []
    body = {"data": {"invitation": {"device_type": "cams", "device_id": cam, "email": "Other@X.io"}}}
    c = await client.post("/v1/invitations", headers=h, json=body)
    assert c.status_code == 201 and c.json()["data"]["invitation"]["email"] == "other@x.io"
    iid = c.json()["data"]["invitation"]["id"]
    r = await client.get("/v1/invitations?search=pending&search_column=status&search_method=equals", headers=h)
    assert r.json()["code"] == "000" and r.json()["data"]["settings"]["total"] == 1
    assert (await client.get("/v1/invitations", headers=bearer(await tok(other)))).json()["data"]["settings"]["total"] == 1
    r = await client.post(f"/v1/invitations/{iid}/accept", headers=bearer(await tok(admin, "admin")))
    assert r.json()["code"] == "002"
    assert (await client.post("/v1/invitations/9999/accept", headers=h)).json()["code"] == "215"
    assert (await client.post("/v1/invitations/9999/decline", headers=h)).json()["code"] == "215"
    assert (await client.get("/v1/invitations/9999", headers=h)).json()["code"] == "215"
    assert (await client.delete("/v1/invitations/9999", headers=h)).json()["code"] == "215"
    assert (await client.get("/v1/shares/9999", headers=h)).json()["code"] == "215"
    assert (await client.delete("/v1/shares/9999", headers=h)).json()["code"] == "215"


async def test_post_bodies_must_be_wrapped(client, setup):
    owner, _, _, cam = setup
    h = bearer(await tok(owner))
    r = await client.post("/v1/shares", headers=h, json={"device_type": "cams", "device_id": cam, "type": "stream"})
    assert r.json()["code"] == "269"
    r = await client.post("/v1/invitations", headers=h, json={"device_type": "cams"})
    assert r.json()["code"] == "269"


async def test_options_lists(client):
    for path in ("/v1/shares", "/v1/invitations", "/v1/cams/1/motion/events"):
        r = await client.options(path)
        assert "GET" in r.headers["allow"], path
        fns = r.headers["cursed-supported-functions"].split(",")
        assert {"GET.limit", "GET.offset", "GET.sort_by", "GET.search"} <= set(fns), path


# ---------------- push settings on switches ----------------
async def test_switch_push_settings(client, setup, sessionmaker_):
    owner, other, admin, _ = setup
    now = int(time.time())
    async with sessionmaker_() as s:
        vals = dict(ownuser=owner, name='sw', state_updated_at=now, added_at=now, updated_at=now)
        sw = Switch(**vals)
        s.add(sw)
        await s.commit()
        sid = sw.id
    url = f"/v1/switches/{sid}/push-settings"
    h = bearer(await tok(owner))
    r = await client.get(url, headers=h)
    assert r.json()["code"] == "000" and r.json()["data"]["push_settings"]["device_type"] == "switches"
    r = await client.patch(url, headers=h, json={"data": {"push_settings": {"notify_online": 0}}})
    assert r.json()["code"] == "002"
    r = await client.patch(url, headers=h, json={"data": {"push_settings": {"notify_online": 0}}})
    assert r.json()["code"] == "007"
    assert (await client.get(url, headers=bearer(await tok(other)))).json()["code"] == "189"
    assert (await client.get(url, headers=bearer(await tok(admin, "admin")))).json()["code"] == "000"
    assert (await client.get("/v1/switches/9999/push-settings", headers=h)).json()["code"] == "215"


# ---------------- motion photo / admin ----------------
async def test_motion_photo_missing_and_admin_delete(client, setup, sessionmaker_):
    owner, other, admin, cam = setup
    async with sessionmaker_() as s:
        ev = MotionEvent(cam_id=cam, detected_at=int(time.time()), object_type="car", confidence=0.8,
                         object_size=0.1, photo_filename="nonexistent_test.jpg")
        s.add(ev)
        await s.commit()
        eid = ev.id
    h = bearer(await tok(owner))
    r = await client.get(f"/v1/motion/events/{eid}/photo", headers=h)
    assert r.json()["code"] == "215"
    assert (await client.get(f"/v1/motion/events/{eid}/photo", headers=bearer(await tok(other)))).json()["code"] == "189"
    assert (await client.get("/v1/motion/events/9999", headers=h)).json()["code"] == "215"
    assert (await client.delete("/v1/motion/events/9999", headers=h)).json()["code"] == "215"
    assert (await client.get("/v1/cams/9999/motion/status", headers=h)).json()["code"] == "215"
    r = await client.delete(f"/v1/motion/events/{eid}", headers=bearer(await tok(admin, "admin")))
    assert r.json()["code"] == "009"


# ---------------- stats ----------------
@pytest.mark.parametrize("path", ["", "/system", "/streams", "/cameras", "/recordings", "/motion",
                                  "/users", "/errors", "/performance"])
async def test_stats_forbidden_for_user_and_requires_auth(client, setup, path):
    owner, *_ = setup
    r = await client.get(f"/v1/stats{path}", headers=bearer(await tok(owner)))
    assert r.status_code == 403 and r.json()["code"] == "186"
    assert (await client.get(f"/v1/stats{path}")).status_code == 401


async def test_stats_db_backed_admin(client, setup, monkeypatch, sessionmaker_):
    from app.services import stats_collector
    _, _, admin, cam = setup
    h = bearer(await tok(admin, "admin"))
    monkeypatch.setattr(stats_collector, "get_system_stats", lambda: {
        "cpu_percent": 1.5, "memory": {"percent": 10.0}, "disk": {"percent": 5.0},
        "network": {"bytes_sent": 1}, "uptime_seconds": 7})
    r = await client.get("/v1/stats/system", headers=h)
    assert r.json()["code"] == "000" and r.json()["data"]["system"]["uptime_seconds"] == 7
    r = await client.get("/v1/stats/cameras", headers=h)
    assert r.json()["data"]["cameras"]["total"] == 1
    r = await client.get("/v1/stats/users", headers=h)
    assert r.json()["data"]["users"]["total_users"] == 3
    r = await client.get("/v1/stats/motion?period=6", headers=h)
    assert r.json()["data"]["motion"]["period_hours"] == 6
    assert (await client.get("/v1/stats/motion?period=0", headers=h)).status_code == 400
    monkeypatch.setattr(stats_collector, "get_performance_stats", lambda: {"endpoints": {}, "snapshots": {}})
    r = await client.get("/v1/stats/performance", headers=h)
    assert r.json()["data"] == {"performance": {"endpoints": {}, "snapshots": {}}}
    # CursedJSON envelope
    assert set(r.json()) == {"code", "code_detail", "error", "metadata", "data"}
