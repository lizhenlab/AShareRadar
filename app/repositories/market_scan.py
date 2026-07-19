from __future__ import annotations

from app.models.market_scan import MarketScanResultWrite, MarketScanRetryPlan, MarketScanSeed
from app.repositories.base import SQLiteRepository
from app.repositories.market_scan_lifecycle import (
    ACTIVE_SCAN_STATUSES,
    RETRYABLE_SCAN_STATUSES,
    TERMINAL_SCAN_STATUSES,
    MarketScanLifecycleMixin,
)
from app.repositories.market_scan_queries import MarketScanQueryMixin
from app.repositories.market_scan_results import MarketScanResultWriterMixin


class MarketScanRepository(
    MarketScanLifecycleMixin,
    MarketScanResultWriterMixin,
    MarketScanQueryMixin,
    SQLiteRepository,
):
    """Stable repository facade composed from cohesive scan persistence concerns."""


__all__ = [
    "ACTIVE_SCAN_STATUSES",
    "MarketScanRepository",
    "MarketScanResultWrite",
    "MarketScanRetryPlan",
    "MarketScanSeed",
    "RETRYABLE_SCAN_STATUSES",
    "TERMINAL_SCAN_STATUSES",
]
