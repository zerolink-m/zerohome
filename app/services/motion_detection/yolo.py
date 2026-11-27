"""Motion detection service using YOLOv8."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

logger = logging.getLogger("app.services.motion_detection")

_yolo_model = None
_model_lock = Lock()
model_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="motion-yolo")


YOLO_CLASSES = {
    0: 'person',
    16: 'dog',
    15: 'cat',
    2: 'car',
}


def _get_yolo_model():
    """Lazy load YOLO model."""
    global _yolo_model
    with _model_lock:
        if _yolo_model is None:
            try:
                from ultralytics import YOLO
                logger.info("Loading YOLOv8 model...")
                _yolo_model = YOLO('yolov8n.pt')  # Nano model (fast)
                logger.info("YOLOv8 model loaded successfully")
            except Exception as exc:
                logger.error(f"Failed to load YOLO model: {exc}")
                raise
        return _yolo_model



def infer(model, frame):
    # The shared Ultralytics predictor may mutate its state during inference.
    with _model_lock:
        return model(frame, verbose=False)[0]
