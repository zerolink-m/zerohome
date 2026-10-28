from __future__ import annotations

from typing import Generic, TypeVar

from app.schemas.base import StrictModel

T = TypeVar("T")


class ListData(StrictModel, Generic[T]):
    """Standard paginated list envelope payload."""

    total: int
    offset: int
    limit: int
    items: list[T]
