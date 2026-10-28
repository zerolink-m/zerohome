"""Motion detection service using YOLOv8."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, Optional

import cv2
import numpy as np

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.cams import Cam
from app.models.motion_event import MotionEvent
from app.models.push_settings import PushSettings
from app.models.user import User
from app.services import license_plate_ocr, push
from app.services.object_tracker import ObjectTracker

logger = logging.getLogger("app.services.motion_detection")

_yolo_model = None


YOLO_CLASSES = {
    0: 'person',
    16: 'dog',
    15: 'cat',
    2: 'car',
}


def _get_yolo_model():
    """Lazy load YOLO model."""
    global _yolo_model
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
