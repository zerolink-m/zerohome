"""MOTION_DEBUG_LOG: one INFO line per inference with the raw YOLO detections and the verdict."""
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.config import settings
from app.services.motion_detection.session import MotionDetectionSession
from app.services.object_tracker import TrackedObject

LOGGER = "app.services.motion_detection"


class _Arr:
    def __init__(self, v):
        self.v = v

    def cpu(self):
        return self

    def numpy(self):
        return np.array(self.v)


def _box(cls, conf, xyxy):
    return SimpleNamespace(cls=[cls], conf=[conf], xyxy=[_Arr(xyxy)])


class _Model:
    """Stub for the YOLO model: returns the boxes it is given, no ultralytics involved."""

    def __init__(self, *boxes):
        self.boxes = list(boxes)

    def __call__(self, frame, verbose=False):
        return [SimpleNamespace(boxes=self.boxes, names={0: "person", 56: "chair"})]


def _session(**kw):
    ps = dict(user_id=1, motion_detect_person=1, motion_detect_dog=1, motion_detect_cat=1, motion_detect_car=1,
              motion_min_confidence=0.6, motion_min_object_size=0.02, motion_cooldown_seconds=0,
              motion_license_plate_ocr=0)
    ps.update(kw)
    cam = SimpleNamespace(id=7, name="c", proto="rtsp", username="u", password="p", addr="x", port=554, path="/")
    session = MotionDetectionSession(cam, [SimpleNamespace(**ps)])
    session.saved = []

    async def save(*args):
        session.saved.append(args)

    session._save_and_notify = save
    return session


FRAME = np.zeros((100, 400, 3), dtype=np.uint8)  # 40000 px


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]


async def test_flag_off_logs_nothing(caplog, monkeypatch):
    monkeypatch.setattr(settings, "MOTION_DEBUG_LOG", False)
    s = _session()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await s._process_frame(FRAME, _Model(_box(0, 0.9, (0, 0, 200, 100))), 1, 1000.0)
    assert not [m for m in _lines(caplog) if "yolo" in m]


async def test_flag_on_reports_drop_reasons(caplog, monkeypatch):
    monkeypatch.setattr(settings, "MOTION_DEBUG_LOG", True)
    s = _session()
    model = _Model(_box(0, 0.41, (10, 10, 110, 90)),     # person below min_conf
                   _box(56, 0.88, (0, 0, 50, 50)),       # chair: not monitored
                   _box(0, 0.90, (0, 0, 5, 5)))          # person, too small
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await s._process_frame(FRAME, model, 3, 1000.0)
    (line,) = [m for m in _lines(caplog) if "yolo" in m]
    print(line)
    assert "[Cam 7] Frame #3 yolo 400x100 infer_ms=" in line
    assert "detections=3" in line
    assert "person(id=0) conf=0.41 bbox=(10,10,100,80)" in line and "dropped: conf<0.60" in line
    assert "chair(id=56) conf=0.88" in line and "dropped: class not in monitored list" in line
    assert "dropped: size<0.0200" in line
    assert "no_event: all detections dropped by filters" in line and "events=0" in line
    assert not s.saved


async def test_flag_on_no_detections_explicit(caplog, monkeypatch):
    monkeypatch.setattr(settings, "MOTION_DEBUG_LOG", True)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await _session()._process_frame(FRAME, _Model(), 1, 1000.0)
    (line,) = [m for m in _lines(caplog) if "yolo" in m]
    assert "detections=0" in line and "no_event: yolo found nothing" in line


async def test_flag_on_tracker_and_event(caplog, monkeypatch):
    monkeypatch.setattr(settings, "MOTION_DEBUG_LOG", True)
    s = _session()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await s._process_frame(FRAME, _Model(_box(0, 0.9, (0, 0, 200, 100))), 1, 1000.0)
        await s._process_frame(FRAME, _Model(_box(0, 0.9, (60, 0, 260, 100))), 2, 1001.0)
    first, second = [m for m in _lines(caplog) if "yolo" in m]
    assert "person(id=0) conf=0.90" in first and "passed filters" in first
    assert "no_event: tracker - no moving object" in first and "events=0" in first
    assert "-> event events=1" in second
    assert len(s.saved) == 1


async def test_flag_on_cooldown(caplog, monkeypatch):
    monkeypatch.setattr(settings, "MOTION_DEBUG_LOG", True)
    s = _session(motion_cooldown_seconds=60)
    s.last_notification_time[(1, "person")] = 1000.0
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await s._process_frame(FRAME, _Model(_box(0, 0.9, (0, 0, 200, 100))), 1, 1000.0)
        await s._process_frame(FRAME, _Model(_box(0, 0.9, (60, 0, 260, 100))), 2, 1001.0)
    assert "no_event: cooldown (person)" in _lines(caplog)[-1]
    assert not s.saved


@pytest.mark.parametrize("debug_log", [False, True])
async def test_moving_object_reaches_save_and_notify(caplog, monkeypatch, debug_log):
    """Regression: the moving-object path used to raise AttributeError (TrackedObject.age)."""
    monkeypatch.setattr(settings, "MOTION_DEBUG_LOG", debug_log)
    s = _session()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await s._process_frame(FRAME, _Model(_box(0, 0.9, (0, 0, 200, 100))), 1, 1000.0)
        await s._process_frame(FRAME, _Model(_box(0, 0.9, (60, 0, 260, 100))), 2, 1001.0)
    assert len(s.saved) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    if debug_log:
        assert "-> event events=1" in _lines(caplog)[-1]


def test_tracked_object_age():
    t = TrackedObject(0, "person", 10.0, 12.5, [(0, 0, 1, 1)], [0.9])
    assert t.age == 2.5
