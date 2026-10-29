"""TrackedObject.is_moving: Euclidean x/y shift between first and last bbox."""
from app.services.object_tracker import TrackedObject


def _obj(first, last):
    return TrackedObject(0, "person", 0.0, 1.0, [(*first, 10, 10), (*last, 10, 10)], [0.9, 0.9])


def test_vertical_only_shift_over_threshold_is_moving():
    assert _obj((100, 100), (100, 170)).is_moving() is True


def test_horizontal_only_shift_over_threshold_is_moving():
    assert _obj((100, 100), (170, 100)).is_moving() is True


def test_small_shift_on_both_axes_is_not_moving():
    assert _obj((100, 100), (130, 130)).is_moving() is False  # ~42 px
