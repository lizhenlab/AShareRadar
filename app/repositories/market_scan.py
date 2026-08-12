from __future__ import annotations

from pathlib import Path
import threading

from app.models.market_scan import MarketScanResultWrite, MarketScanRetryPlan, MarketScanSeed
from app.repositories.base import SQLiteRepository
from app.repositories.market_scan_lifecycle import (
    ACTIVE_SCAN_STATUSES,
    RETRYABLE_SCAN_STATUSES,
    TERMINAL_SCAN_STATUSES,
    MarketScanLifecycleMixin,
)
from app.repositories.market_scan_queries import MarketScanQueryMixin
from app.repositories.market_scan_probability_capture import (
    MarketScanProbabilityCaptureOutboxMixin,
)
from app.repositories.market_scan_results import MarketScanResultWriterMixin


class MarketScanRepository(
    MarketScanLifecycleMixin,
    MarketScanProbabilityCaptureOutboxMixin,
    MarketScanResultWriterMixin,
    MarketScanQueryMixin,
    SQLiteRepository,
):
    """Stable repository facade composed from cohesive scan persistence concerns."""

    def __init__(self, path: Path, lock: threading.RLock) -> None:
        super().__init__(path, lock)
        self._run_started_monotonic: dict[int, float] = {}


__all__ = [
    "ACTIVE_SCAN_STATUSES",
    "MarketScanRepository",
    "MarketScanResultWrite",
    "MarketScanRetryPlan",
    "MarketScanSeed",
    "RETRYABLE_SCAN_STATUSES",
    "TERMINAL_SCAN_STATUSES",
]
