"""MOTION_FILES_DIR setting drives where motion photos are read/deleted/cleaned."""
from tests.conftest import mint_test_token
import os
import time
from pathlib import Path

from app.core import config
from app.core.config import settings
from app.models.motion_event import MotionEvent
from app.services import motion_cleanup
from tests.conftest import bearer


def test_default_resolves_from_project_root(monkeypatch):
    monkeypatch.setattr(settings, "MOTION_FILES_DIR", "motion_files")
    assert settings.motion_files_path == str(config.PROJECT_ROOT / "motion_files")
    assert os.path.isabs(settings.motion_files_path)


def test_absolute_and_relative_override(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MOTION_FILES_DIR", str(tmp_path))
    assert settings.motion_files_path == str(tmp_path)
    monkeypatch.setattr(settings, "MOTION_FILES_DIR", "var/mf")
    assert settings.motion_files_path == str(config.PROJECT_ROOT / "var" / "mf")


def test_no_hardcoded_path_left():
    root = Path(__file__).resolve().parent.parent / "app"
    bad = [str(f) for f in root.rglob("*.py") if "/opt/zerocams/motion_files" in f.read_text()]
    assert not bad, bad


async def _event(sessionmaker_, cam, fname):
    async with sessionmaker_() as s:
        ev = MotionEvent(cam_id=cam, detected_at=int(time.time()), object_type="car", confidence=0.8,
                         object_size=0.1, photo_filename=fname)
        s.add(ev)
        await s.commit()
        return ev.id


async def test_photo_get_and_delete_use_configured_dir(client, mk_user, mk_cam, sessionmaker_, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MOTION_FILES_DIR", str(tmp_path))
    owner = await mk_user("o@x.io")
    cam = await mk_cam(owner)
    (tmp_path / str(cam)).mkdir()
    photo = tmp_path / str(cam) / "p.jpg"
    photo.write_bytes(b"\xff\xd8jpegdata")
    eid = await _event(sessionmaker_, cam, "p.jpg")
    h = bearer(await mint_test_token(owner, "user"))

    r = await client.get(f"/v1/motion/events/{eid}/photo", headers=h)
    assert r.status_code == 200 and r.content == b"\xff\xd8jpegdata"

    r = await client.delete(f"/v1/motion/events/{eid}", headers=h)
    assert r.json()["code"] == "007"
    assert not photo.exists()


async def test_cleanup_uses_configured_dir(mk_user, mk_cam, sessionmaker_, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MOTION_FILES_DIR", str(tmp_path))
    monkeypatch.setattr(motion_cleanup, "AsyncSessionLocal", sessionmaker_)
    owner = await mk_user("o@x.io")
    cam = await mk_cam(owner)
    d = tmp_path / str(cam)
    d.mkdir()
    (d / "old.jpg").write_bytes(b"x")
    async with sessionmaker_() as s:
        s.add(MotionEvent(cam_id=cam, detected_at=int(time.time()) - 8 * 86400, object_type="car",
                          confidence=0.8, object_size=0.1, photo_filename="old.jpg"))
        await s.commit()
    await motion_cleanup._cleanup_once()
    assert not (d / "old.jpg").exists()
    assert not d.exists()  # empty cam dir removed
