"""Compatibility facade for metadata coordination services.

Implementation lives in focused ``datahub_metadata_*`` modules. Existing imports
remain stable through the explicit re-exports below.
"""

from app.services.datahub_metadata_coordinator import (
    MetadataCoordinator,
    PlateRankResult,
    StockConceptResult,
)
from app.services.datahub_metadata_mapping import _profile_with_local_industry as _profile_with_local_industry_impl
from app.services.datahub_metadata_stock_pool import (
    STOCK_POOL_BASELINE_COMPARISON_MIN_COUNT,
    STOCK_POOL_FALLBACK_SECONDS,
    STOCK_POOL_MARKETS,
    STOCK_POOL_MIN_BASELINE_RETAIN_RATIO,
    StockPoolRequest,
    StockPoolResolution,
    StockPoolResolver,
    _stock_pool_markets as _stock_pool_markets_impl,
)


_profile_with_local_industry = _profile_with_local_industry_impl
_stock_pool_markets = _stock_pool_markets_impl


__all__ = [
    "MetadataCoordinator",
    "PlateRankResult",
    "STOCK_POOL_BASELINE_COMPARISON_MIN_COUNT",
    "STOCK_POOL_FALLBACK_SECONDS",
    "STOCK_POOL_MARKETS",
    "STOCK_POOL_MIN_BASELINE_RETAIN_RATIO",
    "StockConceptResult",
    "StockPoolRequest",
    "StockPoolResolution",
    "StockPoolResolver",
]
