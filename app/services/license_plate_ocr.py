"""License plate OCR using EasyOCR."""
from __future__ import annotations

import logging
import re
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Lazy-loaded OCR reader
_reader = None


def _get_reader():
    """Lazy load EasyOCR reader."""
    global _reader
    if _reader is None:
        try:
            import easyocr
            logger.info("Initializing EasyOCR reader for Russian license plates...")
            _reader = easyocr.Reader(['en', 'ru'], gpu=False)
            logger.info("EasyOCR reader initialized successfully")
        except Exception as exc:
            logger.error(f"Failed to initialize EasyOCR: {exc}")
            raise
    return _reader


def extract_license_plate(image: np.ndarray, bbox: tuple[int, int, int, int]) -> Optional[str]:
    """
    Extract license plate text from car bounding box.

    Args:
        image: Full frame (BGR format)
        bbox: Car bounding box (x, y, w, h)

    Returns:
        License plate string or None if not found
    """
    try:
        x, y, w, h = bbox

        # Crop car region
        car_crop = image[y:y+h, x:x+w]

        if car_crop.size == 0:
            return None

        # Focus on bottom third of car (where plates usually are)
        bottom_third_y = int(h * 0.66)
        plate_region = car_crop[bottom_third_y:, :]

        if plate_region.size == 0:
            return None

        # Convert to grayscale
        gray = cv2.cvtColor(plate_region, cv2.COLOR_BGR2GRAY)

        # Apply threshold to make text clearer
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # OCR
        reader = _get_reader()
        results = reader.readtext(thresh, detail=1)

        # Find best matching license plate pattern
        for (bbox_ocr, text, conf) in results:
            if conf < 0.5:  # Skip low confidence
                continue

            # Clean text
            text = text.strip().upper()
            text = re.sub(r'[^A-ZА-Я0-9]', '', text)  # Remove non-alphanumeric

            # Russian license plate patterns:
            # А123БВ45 (private), А123БВ456 (new), АА123456 (taxi), etc.
            if _is_valid_license_plate(text):
                logger.info(f"Detected license plate: {text} (confidence: {conf:.2f})")
                return _format_license_plate(text)

        return None

    except Exception as exc:
        logger.warning(f"License plate OCR failed: {exc}")
        return None


def _is_valid_license_plate(text: str) -> bool:
    """Check if text matches Russian license plate pattern."""
    if len(text) < 6 or len(text) > 9:
        return False

    # Should contain both letters and numbers
    has_letters = any(c.isalpha() for c in text)
    has_numbers = any(c.isdigit() for c in text)

    if not (has_letters and has_numbers):
        return False

    # Common patterns:
    # А123БВ45 - 8 chars (letter, 3 digits, 2 letters, 2 digits)
    # А123БВ456 - 9 chars
    # АА123456 - 8 chars (taxi)

    patterns = [
        r'^[A-ZА-Я]\d{3}[A-ZА-Я]{2}\d{2,3}$',  # А123БВ45 or А123БВ456
        r'^[A-ZА-Я]{2}\d{6}$',  # АА123456 (taxi)
        r'^[A-ZА-Я]\d{4}[A-ZА-Я]{2}$',  # А1234БВ
    ]

    for pattern in patterns:
        if re.match(pattern, text):
            return True

    return False


def _format_license_plate(text: str) -> str:
    """Format license plate for display (add space for readability)."""
    # А123БВ45 -> А123БВ 45
    if len(text) == 8 and text[0].isalpha() and text[-2:].isdigit():
        return f"{text[:6]} {text[6:]}"
    # А123БВ456 -> А123БВ 456
    elif len(text) == 9 and text[0].isalpha() and text[-3:].isdigit():
        return f"{text[:6]} {text[6:]}"

    return text


def is_ocr_available() -> bool:
    """Check if OCR is available."""
    try:
        _get_reader()
        return True
    except Exception:
        return False
