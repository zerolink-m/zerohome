"""Motion detection package: YOLO loader, per-camera session, session registry."""
from app.services.motion_detection.yolo import YOLO_CLASSES, _get_yolo_model  # noqa: F401
from app.services.motion_detection.session import MotionDetectionSession  # noqa: F401
from app.services.motion_detection.manager import (  # noqa: F401
    _active_sessions,
    is_motion_detection_active,
    start_motion_detection,
    stop_all_sessions,
    startup,
    stop_motion_detection,
)
