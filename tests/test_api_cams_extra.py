from tests.conftest import mint_test_token
import os
import time

from app.core.security import create_share_token
from app.models.invitation import Invitation
from app.models.sharing import Sharing
from tests.conftest import bearer


async def tok(uid, role="user"):
    return await mint_test_token(uid, role)


def req(key, obj):
    return {"data": {key: obj}}


async def _invite(db, cam_id, email):
    now = int(time.time())
    async with db() as s:
        s.add(Invitation(device_type="cams", device_id=cam_id, email=email, invited_by=1,
                         status="accepted", added_at=now))
        await s.commit()


async def test_admin_can_patch_and_delete_foreign_cam(client, mk_user, mk_cam):
    owner = await mk_user("o@x.io")
    admin = await mk_user("a@x.io", role="admin")
    cid = await mk_cam(owner)
    h = bearer(await tok(admin, "admin"))
    r = await client.patch(f"/v1/cams/{cid}", headers=h, json=req("cam", {"name": "adm"}))
    assert r.json()["code"] == "002" and r.json()["data"]["cam"]["name"] == "adm"
    r = await client.delete(f"/v1/cams/{cid}", headers=h)
    assert r.status_code == 200 and r.json()["code"] == "009"


async def test_invited_user_forbidden_on_manage_endpoints(client, mk_user, mk_cam, db):
    owner = await mk_user("o@x.io")
    guest = await mk_user("g@x.io")
    cid = await mk_cam(owner)
    await _invite(db, cid, "g@x.io")
    h = bearer(await tok(guest))
    r = await client.get(f"/v1/cams/{cid}/recording", headers=h)
    assert r.status_code == 403 and r.json()["code"] == "189"
    r = await client.patch(f"/v1/cams/{cid}/recording", headers=h, json=req("recording_config", {"record": 0}))
    assert r.status_code == 403 and r.json()["code"] == "189"
    r = await client.patch(f"/v1/cams/{cid}/onvif", headers=h, json=req("onvif", {"onvif_enabled": 1}))
    assert r.status_code == 403 and r.json()["code"] == "189"
    # but the invitee may read PTZ status (control not configured -> 176)
    r = await client.get(f"/v1/cams/{cid}/ptz", headers=h)
    assert r.status_code == 409 and r.json()["code"] == "388"


async def test_unknown_cam_codes(client, mk_user):
    h = bearer(await tok(await mk_user("o@x.io")))
    for method, path, body in (
        ("get", "/v1/cams/999/ptz", None),
        ("get", "/v1/cams/999/recording", None),
        ("get", "/v1/cams/999/recordings", None),
        ("get", "/v1/cams/999/recordings/a.mp4", None),
        ("patch", "/v1/cams/999/onvif", req("onvif", {"onvif_enabled": 1})),
        ("patch", "/v1/cams/999/recording", req("recording_config", {"record": 0})),
    ):
        r = await getattr(client, method)(path, headers=h, **({"json": body} if body else {}))
        assert r.status_code == 404 and r.json()["code"] == "215", path


async def test_recording_not_configured_177(client, mk_user, mk_cam):
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    r = await client.get(f"/v1/cams/{cid}/recordings", headers=bearer(await tok(owner)))
    assert r.status_code == 409 and r.json()["code"] == "389"


async def test_empty_list_has_settings_and_012(client, mk_user):
    r = await client.get("/v1/cams", headers=bearer(await tok(await mk_user("o@x.io"))))
    j = r.json()
    assert r.status_code == 200 and j["code"] == "019"
    assert j["data"]["cams"] == [] and j["data"]["settings"]["total"] == 0


async def test_snapshot_and_stream_errors_are_json(client, mk_user, mk_cam, monkeypatch):
    from app.services import snapshot, stream_manager
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    h = bearer(await tok(owner))

    async def bad_snap(cam):
        raise snapshot.SnapshotError("boom")

    async def ok_snap(cam):
        return b"\xff\xd8jpeg"

    monkeypatch.setattr(snapshot, "get_live_snapshot", bad_snap)
    r = await client.get(f"/v1/cams/{cid}/snapshot", headers=h)
    assert r.status_code == 500 and r.json()["code"] == "332"
    monkeypatch.setattr(snapshot, "get_live_snapshot", ok_snap)
    r = await client.get(f"/v1/cams/{cid}/snapshot", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == b"\xff\xd8jpeg"

    async def no_session(cam_id):
        return False

    async def fail_attach(cam):
        raise stream_manager.StreamStartError("ffmpeg down")

    monkeypatch.setattr(stream_manager, "has_active_session", no_session)
    monkeypatch.setattr(stream_manager, "get_or_attach", fail_attach)
    r = await client.get(f"/v1/cams/{cid}/stream", headers=h)
    assert r.status_code == 500 and r.json()["code"] == "332"
    r = await client.get("/v1/cams/999/stream", headers=h)
    assert r.status_code == 404 and r.json()["code"] == "215"


async def test_hls_disabled_and_segment_errors(client, mk_user, mk_cam, monkeypatch, tmp_path):
    from app.core.config import settings
    from app.services import hls_manager
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    h = bearer(await tok(owner))
    r = await client.get(f"/v1/cams/{cid}/hls/playlist.m3u8", headers=h)
    assert r.status_code == 400 and r.json()["code"] == "059"
    r = await client.get(f"/v1/cams/{cid}/hls/seg1.ts", headers=h)
    assert r.json()["code"] == "059"

    monkeypatch.setattr(settings, "HLS_ENABLED", True)

    class FakeSession:
        temp_dir = tmp_path
        playlist_path = tmp_path / "playlist.m3u8"

        def touch(self):
            pass

    async def fake_session(cam):
        return FakeSession()

    monkeypatch.setattr(hls_manager, "get_or_create_session", fake_session)
    r = await client.get(f"/v1/cams/{cid}/hls/missing.ts", headers=h)
    assert r.status_code == 404 and r.json()["code"] == "219"
    r = await client.get(f"/v1/cams/{cid}/hls/notts.txt", headers=h)
    assert r.json()["code"] == "219"
    (tmp_path / "s0.ts").write_bytes(b"TS")
    r = await client.get(f"/v1/cams/{cid}/hls/s0.ts", headers=h)
    assert r.status_code == 200 and r.content == b"TS"  # binary untouched


async def test_record_share_token_gets_only_its_file(client, mk_user, mk_cam, db, tmp_path):
    from app.models.user import User
    from sqlalchemy import update
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    h = bearer(await tok(owner))
    async with db() as s:
        await s.execute(update(User).where(User.id == owner).values(allowrecord=1))
        await s.commit()
    cfg = req("recording_config", {"record": 1, "record_type": "local",
                                   "record_metadata": {"local": {"path": str(tmp_path)}}})
    assert (await client.patch(f"/v1/cams/{cid}/recording", headers=h, json=cfg)).json()["code"] == "002"
    (tmp_path / "a.mp4").write_bytes(b"AAA")
    (tmp_path / "b.mp4").write_bytes(b"BBB")
    now = int(time.time())
    async with db() as s:
        sh = Sharing(uid=owner, type="record", device_type="cams", device_id=cid, filename="a.mp4",
                     added_at=now, updated_at=now)
        s.add(sh)
        await s.commit()
        sid = sh.id
    t = create_share_token(sid, owner, "record", "cams", cid, "a.mp4", 100)
    r = await client.get(f"/v1/cams/{cid}/recordings/a.mp4/file", headers=bearer(t))
    assert r.status_code == 200 and r.content == b"AAA"
    r = await client.get(f"/v1/cams/{cid}/recordings/b.mp4/file", headers=bearer(t))
    assert r.json()["code"] == "167"
    # a record share does not open JSON listing
    r = await client.get(f"/v1/cams/{cid}/recordings", headers=bearer(t))
    assert r.status_code == 401


async def test_recording_snapshot_error_is_json(client, mk_user, mk_cam, db, tmp_path, monkeypatch):
    from app.models.user import User
    from app.services import snapshot
    from sqlalchemy import update
    owner = await mk_user("o@x.io")
    cid = await mk_cam(owner)
    h = bearer(await tok(owner))
    async with db() as s:
        await s.execute(update(User).where(User.id == owner).values(allowrecord=1))
        await s.commit()
    cfg = req("recording_config", {"record": 1, "record_type": "local",
                                   "record_metadata": {"local": {"path": str(tmp_path)}}})
    await client.patch(f"/v1/cams/{cid}/recording", headers=h, json=cfg)
    (tmp_path / "a.mp4").write_bytes(b"AAA")

    async def bad(*a, **k):
        raise snapshot.SnapshotError("nope")

    monkeypatch.setattr(snapshot, "get_cached_snapshot", bad)
    r = await client.get(f"/v1/cams/{cid}/recordings/a.mp4/snapshot", headers=h)
    assert r.status_code == 500 and r.json()["code"] == "332"
    r = await client.get(f"/v1/cams/{cid}/recordings/zzz.mp4/snapshot", headers=h)
    assert r.status_code == 404 and r.json()["code"] == "215"
