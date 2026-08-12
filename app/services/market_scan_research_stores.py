"""Explicit ownership of file-backed market-scan research projections."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.services.market_scan_future_range_store import MarketScanFutureRangeStore
from app.services.market_scan_probability_capture import PROBABILITY_SOURCE_ARCHIVE_RELATIVE_PATH
from app.services.market_scan_probability_maintenance import (
    PROBABILITY_OUTCOME_ARCHIVE_RELATIVE_PATH,
)
from app.services.market_scan_probability_fit_assessment import (
    PROBABILITY_FIT_ASSESSMENT_RELATIVE_PATH,
)
from app.services.market_scan_probability_source_research import (
    MarketScanProbabilitySourceResearchStore,
)
from app.services.market_scan_probability_store import MarketScanProbabilityStore


@dataclass(frozen=True)
class MarketScanResearchStores:
    probability: MarketScanProbabilityStore | None
    probability_source: MarketScanProbabilitySourceResearchStore | None
    future_range: MarketScanFutureRangeStore | None

    @classmethod
    def for_cache_path(cls, cache_path: object) -> MarketScanResearchStores:
        if not isinstance(cache_path, str | Path):
            return cls(probability=None, probability_source=None, future_range=None)
        data_directory = Path(cache_path).parent
        return cls(
            probability=MarketScanProbabilityStore(data_directory / "market-scan-probability"),
            probability_source=MarketScanProbabilitySourceResearchStore(
                data_directory / PROBABILITY_SOURCE_ARCHIVE_RELATIVE_PATH,
                outcome_directory=data_directory / PROBABILITY_OUTCOME_ARCHIVE_RELATIVE_PATH,
                fit_directory=data_directory / PROBABILITY_FIT_ASSESSMENT_RELATIVE_PATH,
            ),
            future_range=MarketScanFutureRangeStore(
                data_directory / "research" / "market_scan_future_range"
            ),
        )


__all__ = ["MarketScanResearchStores"]
