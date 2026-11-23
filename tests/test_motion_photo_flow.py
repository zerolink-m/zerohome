"""Motion photo state, legacy files, and a notification that waits for the upload."""
import time
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import settings
from app.models.motion_event import MotionEvent
from app.models.cams import Cam
from app.models.notification import NotificationJob
from app.models.user import User
from app.services import motion_cleanup, notify_worker
from app.services.motion_photos import save_motion_event_and_photo
from app.services.storage.local import LocalStorage
from app.routers.motion import delete_motion_event
from app.core.errors import AppError
from sqlalchemy import select

from tests.conftest import bearer, mint_test_token


class _Store:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = 0
        self.data: dict[str, bytes] = {}
        self.states: list[str | None] = []
        self.sessionmaker = None

    async def put(self, key, data, content_type):
        self.calls += 1
        if self.sessionmaker is not None:
            async with self.sessionmaker() as db:
                event = (await db.execute(select(MotionEvent))).scalar_one()
                self.states.append(event.photo_state)
        if self.calls <= self.fail_times:
            raise OSError("disk full")
        self.data[key] = bytes(data)

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def exists(self, key):
        return key in self.data


def _patch_store(monkeypatch, sessionmaker_, store: _Store):
    store.sessionmaker = sessionmaker_
    monkeypatch.setattr("app.services.motion_photos.AsyncSessionLocal", sessionmaker_)
    monkeypatch.setattr("app.services.storage.get_storage", lambda: store)


async def _save(jpeg=b"\xff\xd8pic"):
    return await save_motion_event_and_photo(
        cam_id=3, detected_at=1_700_000_000, object_type="person", confidence=0.8,
        object_size=0.1, license_plate=None, photo_filename="1700000000_person_80.jpg",
        bbox=(1, 2, 3, 4), notified_user_ids=[9], message="detected", jpeg=jpeg,
    )


async def test_event_uploading_then_ready(sessionmaker_, monkeypatch):
    store = _Store()
    _patch_store(monkeypatch, sessionmaker_, store)
    event_id = await _save()
    assert store.calls == 1 and store.states == ["uploading"]
    async with sessionmaker_() as db:
        event = await db.get(MotionEvent, event_id)
        job = (await db.execute(select(NotificationJob))).scalar_one()
    assert event.photo_state == "ready"
    assert event.photo_key == "motion/3/1700000000_person_80.jpg"
    assert event.photo_filename == "1700000000_person_80.jpg"
    assert store.data[event.photo_key] == b"\xff\xd8pic"
    assert job.photo_key == event.photo_key and job.kind == "motion" and job.state == "pending"
    assert job.payload["object_type"] == "person" and job.payload["user_ids"] == [9]


async def test_upload_failure_retries_once_then_failed(sessionmaker_, monkeypatch):
    store = _Store(fail_times=2)
    _patch_store(monkeypatch, sessionmaker_, store)
    event_id = await _save()
    assert store.calls == 2 and store.data == {}
    async with sessionmaker_() as db:
        event = await db.get(MotionEvent, event_id)
        jobs = (await db.execute(select(NotificationJob))).scalars().all()
    assert event.photo_state == "failed"
    assert len(jobs) == 1 and jobs[0].state == "pending"


async def test_upload_second_attempt_succeeds(sessionmaker_, monkeypatch):
    store = _Store(fail_times=1)
    _patch_store(monkeypatch, sessionmaker_, store)
    event_id = await _save()
    assert store.calls == 2
    async with sessionmaker_() as db:
        event = await db.get(MotionEvent, event_id)
    assert event.photo_state == "ready"
    assert store.data[event.photo_key] == b"\xff\xd8pic"


async def test_legacy_event_served_from_photo_filename(
    client, mk_user, mk_cam, sessionmaker_, monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings, "MOTION_FILES_DIR", str(tmp_path))
    owner = await mk_user("legacy@x.io")
    cam = await mk_cam(owner)
    (tmp_path / str(cam)).mkdir()
    (tmp_path / str(cam) / "old.jpg").write_bytes(b"\xff\xd8legacy")
    async with sessionmaker_() as db:
        event = MotionEvent(
            cam_id=cam, detected_at=int(time.time()), object_type="car",
            confidence=0.5, object_size=0.2, photo_filename="old.jpg",
        )
        db.add(event)
        await db.commit()
        event_id = event.id
    headers = bearer(await mint_test_token(owner, "user"))
    response = await client.get(f"/v1/motion/events/{event_id}/photo", headers=headers)
    assert response.status_code == 200 and response.content == b"\xff\xd8legacy"
    body = (await client.get(f"/v1/motion/events/{event_id}", headers=headers)).json()
    assert body["data"]["motion_event"]["photo_filename"] == "old.jpg"
    assert "photo_key" not in str(body) and "photo_state" not in str(body)


async def test_photo_key_roundtrip_hides_key_from_api(
    client, mk_user, mk_cam, sessionmaker_, monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings, "MOTION_FILES_DIR", str(tmp_path))
    owner = await mk_user("new@x.io")
    cam = await mk_cam(owner)
    storage = LocalStorage(str(tmp_path))
    await storage.put(f"motion/{cam}/p.jpg", b"\xff\xd8new", "image/jpeg")
    async with sessionmaker_() as db:
        event = MotionEvent(
            cam_id=cam, detected_at=int(time.time()), object_type="person",
            confidence=0.9, object_size=0.2, photo_filename="p.jpg",
            photo_key=f"motion/{cam}/p.jpg", photo_state="ready",
        )
        db.add(event)
        await db.commit()
        event_id = event.id
    headers = bearer(await mint_test_token(owner, "user"))
    photo = await client.get(f"/v1/motion/events/{event_id}/photo", headers=headers)
    assert photo.status_code == 200 and photo.content == b"\xff\xd8new"
    # The legacy path was never written; the bytes came from the object key.
    assert not (tmp_path / str(cam) / "p.jpg").exists()
    body = (await client.get(f"/v1/motion/events/{event_id}", headers=headers)).text
    assert "photo_key" not in body and "photo_state" not in body
    deleted = await client.delete(f"/v1/motion/events/{event_id}", headers=headers)
    assert deleted.json()["code"] == "009"
    assert await storage.get(f"motion/{cam}/p.jpg") is None


class _DeleteDB:
    def __init__(self, event, cam):
        self.event = event
        self.cam = cam
        self.committed = False

    async def get(self, model, key):
        if model is MotionEvent and self.event.id == key:
            return self.event
        if model is Cam and self.cam.id == key:
            return self.cam
        return None

    async def delete(self, event):
        assert event is self.event
        self.event = None

    async def commit(self):
        self.committed = True


def _delete_fixture(photo_key=None, photo_filename=None):
    event = MotionEvent(id=71, cam_id=9, detected_at=1, object_type="person",
                        confidence=0.9, object_size=0.1,
                        photo_key=photo_key, photo_filename=photo_filename)
    cam = Cam(id=9, ownuser=11)
    return event, cam, _DeleteDB(event, cam)


async def test_delete_motion_event_keeps_row_when_storage_delete_fails(monkeypatch):
    event, cam, db = _delete_fixture(photo_key="motion/9/p.jpg")

    class BrokenStorage:
        async def delete(self, key):
            raise OSError("storage unavailable")

    monkeypatch.setattr("app.routers.motion.storage_layer.get_storage", lambda: BrokenStorage())
    with pytest.raises(AppError) as exc:
        await delete_motion_event(71, SimpleNamespace(uid=11, role="user"), db)

    assert exc.value.code == "336"
    assert db.event is event and not db.committed


async def test_delete_motion_event_is_idempotent_when_photo_is_missing(monkeypatch, tmp_path):
    event, cam, db = _delete_fixture(photo_filename="already-gone.jpg")
    monkeypatch.setattr(settings, "MOTION_FILES_DIR", str(tmp_path))

    response = await delete_motion_event(71, SimpleNamespace(uid=11, role="user"), db)

    assert response.status_code == 200
    assert db.event is None and db.committed


def _http(monkeypatch, recorder):
    real = httpx.AsyncClient

    def handler(request: httpx.Request):
        recorder.append(request)
        return httpx.Response(200, json={"ok": True})

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.services.push.httpx.AsyncClient", factory)


async def test_notification_waits_while_uploading_then_sends_photo(
    sessionmaker_, monkeypatch, mk_user,
):
    owner = await mk_user("push@x.io")
    async with sessionmaker_() as db:
        user = await db.get(User, owner)
        user.push = "telegram"
        user.pushmeta = {"telegram": {"bot_token": "123456:SECRET-TOKEN", "telegram_id": 7}}
        event = MotionEvent(
            cam_id=1, detected_at=1_000, object_type="person", confidence=0.8,
            object_size=0.1, photo_filename="a.jpg", photo_key="motion/1/a.jpg",
            photo_state="uploading",
        )
        db.add(event)
        job = NotificationJob(
            kind="motion", device_type="cams", device_id=1, message="person on cam",
            photo_key="motion/1/a.jpg", payload={"object_type": "person", "user_ids": [owner]},
            state="pending", attempts=0, available_at=1_000, created_at=1_000, updated_at=1_000,
        )
        db.add(job)
        await db.commit()
        job_id = job.id

    store = _Store()
    monkeypatch.setattr("app.services.storage.get_storage", lambda: store)
    monkeypatch.setattr(notify_worker, "AsyncSessionLocal", sessionmaker_)
    calls: list[httpx.Request] = []
    _http(monkeypatch, calls)

    assert await notify_worker.deliver_batch(now=1_000, node_id="n1") == 1
    assert calls == []
    waiting = await _job(sessionmaker_, job_id)
    assert waiting.state == "pending" and waiting.attempts == 0 and waiting.available_at == 1_002

    store.data["motion/1/a.jpg"] = b"\xff\xd8ready"
    async with sessionmaker_() as db:
        event = (await db.execute(select(MotionEvent))).scalar_one()
        event.photo_state = "ready"
        await db.commit()
    assert await notify_worker.deliver_batch(now=1_002, node_id="n1") == 1
    assert len(calls) == 1 and calls[0].url.path.endswith("/sendPhoto")
    assert b"\xff\xd8ready" in calls[0].content
    sent = await _job(sessionmaker_, job_id)
    assert sent.state == "sent"


async def _job(sm, job_id):
    async with sm() as db:
        return await db.get(NotificationJob, job_id)


async def test_cleanup_removes_storage_object_and_legacy_file(
    sessionmaker_, monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings, "MOTION_FILES_DIR", str(tmp_path))
    monkeypatch.setattr(motion_cleanup, "AsyncSessionLocal", sessionmaker_)
    storage = LocalStorage(str(tmp_path))
    await storage.put("motion/4/new.jpg", b"new", "image/jpeg")
    legacy_dir = tmp_path / "4"
    legacy_dir.mkdir()
    (legacy_dir / "old.jpg").write_bytes(b"old")
    old_ts = int(time.time()) - 8 * 86400
    async with sessionmaker_() as db:
        db.add(MotionEvent(
            cam_id=4, detected_at=old_ts, object_type="car", confidence=0.5,
            object_size=0.1, photo_filename="new.jpg", photo_key="motion/4/new.jpg",
            photo_state="ready",
        ))
        db.add(MotionEvent(
            cam_id=4, detected_at=old_ts, object_type="car", confidence=0.5,
            object_size=0.1, photo_filename="old.jpg",
        ))
        await db.commit()
    await motion_cleanup._cleanup_once()
    assert await storage.get("motion/4/new.jpg") is None
    assert not (legacy_dir / "old.jpg").exists()
    async with sessionmaker_() as db:
        assert (await db.execute(select(MotionEvent))).scalars().all() == []
