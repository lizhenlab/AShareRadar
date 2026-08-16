"""Stable domain contracts consumed by the market-scan persistence boundary.

Repositories depend on this facade rather than reaching into orchestration
services.  The underlying implementations remain shared with the scoring and
runtime paths, so persistence replay cannot drift from production semantics.
"""

from app.services.market_scan_modes import (
    MarketScanTemporalContract,
    market_scan_temporal_contract,
)
from app.services.market_scan_replay import (
    MarketScanScoreReplay,
    verify_score_details,
)
from app.services.market_scan_scoring import (
    FULL_MARKET_SCORE_RULE_VERSION,
    FULL_MARKET_SCORE_SPEC_SCHEMA_VERSION,
    MARKET_SCAN_PRODUCTION_SCORE_SEMANTICS,
    is_current_market_scan_score_spec,
    verify_persisted_market_scan_result,
)
from app.services.market_scan_score_contract import stable_score_spec_hash
from app.services.market_scan_skip_contract import (
    MARKET_SCAN_SKIP_EVIDENCE_KEY,
    verify_market_scan_skip_evidence,
)
from app.services.market_scan_publication_snapshot import (
    snapshot_publication_diagnostics,
)


MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS = 20 * 60
MARKET_SCAN_PUBLISH_MIN_COVERAGE = {
    "ALL": 0.95,
    "SH": 0.95,
    "SZ": 0.95,
    "BJ": 0.95,
}
MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO = {
    "ALL": 0.90,
    "SH": 0.90,
    "SZ": 0.90,
    "BJ": 0.90,
}


__all__ = [
    "FULL_MARKET_SCORE_SPEC_SCHEMA_VERSION",
    "FULL_MARKET_SCORE_RULE_VERSION",
    "MARKET_SCAN_PRODUCTION_SCORE_SEMANTICS",
    "MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS",
    "MARKET_SCAN_PUBLISH_MIN_COVERAGE",
    "MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO",
    "MARKET_SCAN_SKIP_EVIDENCE_KEY",
    "MarketScanScoreReplay",
    "MarketScanTemporalContract",
    "is_current_market_scan_score_spec",
    "market_scan_temporal_contract",
    "snapshot_publication_diagnostics",
    "stable_score_spec_hash",
    "verify_persisted_market_scan_result",
    "verify_market_scan_skip_evidence",
    "verify_score_details",
]
