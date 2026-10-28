"""Object tracker for motion detection - tracks moving objects between frames."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass
class TrackedObject:
    """Tracked object across frames."""
    object_id: int
    object_type: str
    first_seen: float
    last_seen: float
    positions: list[Tuple[int, int, int, int]]  # (x, y, w, h) history
    confidences: list[float]

    @property
    def age(self) -> float:
        """Track lifetime in seconds (last_seen - first_seen)."""
        return self.last_seen - self.first_seen

    def is_moving(self, movement_threshold: float = 50.0) -> bool:
        """Check if object has moved significantly."""
        if len(self.positions) < 2:
            return False

        # Compare first and last position
        first_x, first_y, _, _ = self.positions[0]
        last_x, last_y, _, _ = self.positions[-1]

        distance = ((last_x - first_x) ** 2 + (last_y - last_y) ** 2) ** 0.5
        return distance > movement_threshold

    def get_average_confidence(self) -> float:
        """Get average confidence across all detections."""
        if not self.confidences:
            return 0.0
        return sum(self.confidences) / len(self.confidences)

    def get_latest_bbox(self) -> Tuple[int, int, int, int]:
        """Get latest bounding box."""
        return self.positions[-1] if self.positions else (0, 0, 0, 0)


class ObjectTracker:
    """Simple object tracker based on IoU (Intersection over Union)."""

    def __init__(self, max_age: float = 2.0, iou_threshold: float = 0.3):
        """
        Args:
            max_age: Maximum time (seconds) to keep tracking inactive object
            iou_threshold: Minimum IoU to consider same object
        """
        self.max_age = max_age
        self.iou_threshold = iou_threshold
        self.next_id = 0
        self.tracked_objects: Dict[int, TrackedObject] = {}

    def update(
        self,
        detections: list[Tuple[str, Tuple[int, int, int, int], float]],
        now: float | None = None,
    ) -> list[TrackedObject]:
        """
        Update tracker with new detections.

        Args:
            detections: List of (object_type, bbox, confidence)
                bbox = (x, y, w, h)
            now: capture time of the frame (default: the wall clock); frames that reach
                us in a burst (hub source) carry their own estimated capture times

        Returns:
            List of currently tracked objects
        """
        current_time = time.time() if now is None else now

        # Remove old objects
        self._cleanup_old_objects(current_time)

        # Match detections to existing tracks
        matched_tracks = set()

        for obj_type, bbox, confidence in detections:
            best_match_id = None
            best_iou = 0.0

            # Find best matching existing track
            for obj_id, tracked in self.tracked_objects.items():
                if tracked.object_type != obj_type:
                    continue

                iou = self._calculate_iou(bbox, tracked.get_latest_bbox())
                if iou > best_iou and iou > self.iou_threshold:
                    best_iou = iou
                    best_match_id = obj_id

            if best_match_id is not None:
                # Update existing track
                tracked = self.tracked_objects[best_match_id]
                tracked.last_seen = current_time
                tracked.positions.append(bbox)
                tracked.confidences.append(confidence)
                matched_tracks.add(best_match_id)
            else:
                # Create new track
                new_obj = TrackedObject(
                    object_id=self.next_id,
                    object_type=obj_type,
                    first_seen=current_time,
                    last_seen=current_time,
                    positions=[bbox],
                    confidences=[confidence],
                )
                self.tracked_objects[self.next_id] = new_obj
                matched_tracks.add(self.next_id)
                self.next_id += 1

        return list(self.tracked_objects.values())

    def get_moving_objects(self, min_tracking_time: float = 0.5, now: float | None = None) -> list[TrackedObject]:
        """Get objects that are moving and tracked for minimum time."""
        current_time = time.time() if now is None else now
        moving = []

        for tracked in self.tracked_objects.values():
            # Check if tracked long enough
            tracking_duration = current_time - tracked.first_seen
            if tracking_duration < min_tracking_time:
                continue

            # Check if moving
            if tracked.is_moving():
                moving.append(tracked)

        return moving

    def _cleanup_old_objects(self, current_time: float) -> None:
        """Remove objects that haven't been seen recently."""
        to_remove = []
        for obj_id, tracked in self.tracked_objects.items():
            if current_time - tracked.last_seen > self.max_age:
                to_remove.append(obj_id)

        for obj_id in to_remove:
            del self.tracked_objects[obj_id]

    def _calculate_iou(
        self,
        bbox1: Tuple[int, int, int, int],
        bbox2: Tuple[int, int, int, int]
    ) -> float:
        """Calculate Intersection over Union (IoU) between two bounding boxes."""
        x1, y1, w1, h1 = bbox1
        x2, y2, w2, h2 = bbox2

        # Calculate intersection area
        x_left = max(x1, x2)
        y_top = max(y1, y2)
        x_right = min(x1 + w1, x2 + w2)
        y_bottom = min(y1 + h1, y2 + h2)

        if x_right < x_left or y_bottom < y_top:
            return 0.0

        intersection_area = (x_right - x_left) * (y_bottom - y_top)

        # Calculate union area
        bbox1_area = w1 * h1
        bbox2_area = w2 * h2
        union_area = bbox1_area + bbox2_area - intersection_area

        if union_area == 0:
            return 0.0

        return intersection_area / union_area

    def reset(self) -> None:
        """Reset tracker."""
        self.tracked_objects.clear()
        self.next_id = 0
