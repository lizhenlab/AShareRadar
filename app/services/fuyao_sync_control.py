"""Thread-safe cooperative cancellation and measured sync stage progress."""

from __future__ import annotations

import threading
from typing import TypedDict


class FuyaoSyncCancelled(Exception):
    """An explicit stop request, never a provider or data validation failure."""


class FuyaoSyncSnapshot(TypedDict):
    stage: str
    current: int
    total: int | None
    unit: str | None


class FuyaoSyncControl:
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._progress: FuyaoSyncSnapshot = {"stage": "pending", "current": 0, "total": None, "unit": None}

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    def checkpoint(self, stage: str | None = None, current: int | None = None,
                   total: int | None = None, unit: str | None = None) -> None:
        with self._lock:
            if self.cancelled:
                raise FuyaoSyncCancelled("扶摇同步已请求停止")
            if stage is not None and stage != self._progress["stage"]:
                self._progress = {"stage": stage, "current": 0, "total": None, "unit": None}
            if current is not None:
                self._progress["current"] = current
            if total is not None:
                self._progress["total"] = total
            if unit is not None:
                self._progress["unit"] = unit

    def snapshot(self) -> FuyaoSyncSnapshot:
        with self._lock:
            return self._progress.copy()
