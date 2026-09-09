"""Stable score hashing and frozen trend-context contracts shared by replay."""

from __future__ import annotations

import hashlib
import json

from app.models.market_scan import MarketScanMode
from app.services.indicator_trend_components import (
    TREND_SCORE_ALGORITHM_VERSION,
    TREND_SCORE_LEGACY_ALGORITHM_VERSION,
    TREND_RELATIVE_PCT_DECIMALS,
    TREND_SCALED_IMPACT_DECIMALS,
)


MARKET_SCAN_LEGACY_TREND_ALGORITHM_VERSION = TREND_SCORE_LEGACY_ALGORITHM_VERSION
MARKET_SCAN_ROUNDED_MA_TREND_ALGORITHM_VERSION = "market-scan-trend-v3-completed-snapshot"
MARKET_SCAN_TREND_ALGORITHM_VERSION = "market-scan-trend-v4-scale-invariant-completed-snapshot"


class MarketScanReplayError(ValueError):
    pass


def market_scan_trend_context_mode(
    mode: MarketScanMode, *, algorithm_version: object,
) -> MarketScanMode:
    """Resolve only admitted full-market snapshots under their frozen algorithm.

    A preopen scan has already bound its quote to the previous completed bar.
    The shared individual-stock preopen context can instead contain a current
    auction quote, so its neutral volume policy must remain unchanged.
    """
    if mode not in {"official", "preopen", "intraday"}:
        raise MarketScanReplayError(f"未知全市场趋势模式：{mode!r}")
    market_scan_shared_trend_algorithm(algorithm_version)
    if algorithm_version == MARKET_SCAN_LEGACY_TREND_ALGORITHM_VERSION:
        return mode
    return "official" if mode == "preopen" else mode


def market_scan_shared_trend_algorithm(algorithm_version: object) -> str:
    """Bind each frozen full-market version to its own price precision."""
    if algorithm_version == MARKET_SCAN_TREND_ALGORITHM_VERSION:
        return TREND_SCORE_ALGORITHM_VERSION
    if algorithm_version in (MARKET_SCAN_LEGACY_TREND_ALGORITHM_VERSION, MARKET_SCAN_ROUNDED_MA_TREND_ALGORITHM_VERSION):
        return TREND_SCORE_LEGACY_ALGORITHM_VERSION
    raise MarketScanReplayError(f"未知全市场趋势算法：{algorithm_version!r}")


def market_scan_trend_calculation_spec() -> dict[str, object]:
    return {
        "moving_averages": "unrounded-price-means",
        "relative_pct_decimals": TREND_RELATIVE_PCT_DECIMALS,
        "scaled_impact_decimals": TREND_SCALED_IMPACT_DECIMALS,
        "integer_impact": "floor-stable-scaled-magnitude-plus-half",
    }


def stable_score_spec_hash(spec: object) -> str:
    try:
        canonical = json.dumps(
            spec,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise MarketScanReplayError("评分规范损坏：不是有限、可序列化的 JSON") from exc
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "MARKET_SCAN_LEGACY_TREND_ALGORITHM_VERSION",
    "MARKET_SCAN_ROUNDED_MA_TREND_ALGORITHM_VERSION",
    "MARKET_SCAN_TREND_ALGORITHM_VERSION",
    "MarketScanReplayError",
    "market_scan_trend_context_mode",
    "market_scan_shared_trend_algorithm",
    "market_scan_trend_calculation_spec",
    "stable_score_spec_hash",
]
