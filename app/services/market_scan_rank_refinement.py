from __future__ import annotations

from dataclasses import dataclass

from app.models.market import Kline, Quote


MARKET_SCAN_RANK_REFINEMENT_ALGORITHM_VERSION = "bounded-medium-term-refinement-v1"
MARKET_SCAN_RANK_REFINEMENT_INPUT_DECIMALS = 6
MARKET_SCAN_RANK_REFINEMENT_SCORE_DECIMALS = 6
MARKET_SCAN_RANK_REFINEMENT_MAX_DISCOUNT = 0.0499

MARKET_SCAN_RANK_REFINEMENT_BOUNDS: dict[str, tuple[float, float]] = {
    "close_vs_ma5_pct": (-5.0, 5.0),
    "ma5_vs_ma20_pct": (-8.0, 8.0),
    "ma20_vs_ma60_pct": (-15.0, 15.0),
    "range_position_20d": (0.0, 1.0),
    "return_5d_pct": (-10.0, 10.0),
    "return_20d_pct": (-25.0, 25.0),
}
MARKET_SCAN_RANK_REFINEMENT_WEIGHTS: dict[str, float] = {
    "ma_alignment": 0.40,
    "range_position_20d": 0.25,
    "return_20d_pct": 0.25,
    "return_5d_pct": 0.10,
}


@dataclass(frozen=True)
class MarketScanRankRefinement:
    raw_inputs: dict[str, float]
    normalized_inputs: dict[str, float]
    components: dict[str, float]
    weighted_terms: dict[str, float]
    score: float


def market_scan_rank_refinement(quote: Quote, rows: list[Kline]) -> MarketScanRankRefinement:
    closes = [row.close for row in rows]
    current = quote.price
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    recent_20 = rows[-20:]
    low20 = min(row.low for row in recent_20)
    high20 = max(row.high for row in recent_20)
    raw_inputs = {
        "close_vs_ma5_pct": _pct_change(current, ma5),
        "ma5_vs_ma20_pct": _pct_change(ma5, ma20),
        "ma20_vs_ma60_pct": _pct_change(ma20, ma60),
        "range_position_20d": _range_position(current, low20, high20),
        "return_5d_pct": _pct_change(current, closes[-6]),
        "return_20d_pct": _pct_change(current, closes[-21]),
    }
    rounded_inputs = {
        name: round(value, MARKET_SCAN_RANK_REFINEMENT_INPUT_DECIMALS)
        for name, value in raw_inputs.items()
    }
    normalized = {
        name: _bounded_linear(rounded_inputs[name], *bounds)
        for name, bounds in MARKET_SCAN_RANK_REFINEMENT_BOUNDS.items()
    }
    components = {
        "ma_alignment": round(
            (
                normalized["close_vs_ma5_pct"]
                + normalized["ma5_vs_ma20_pct"]
                + normalized["ma20_vs_ma60_pct"]
            )
            / 3,
            MARKET_SCAN_RANK_REFINEMENT_SCORE_DECIMALS,
        ),
        "range_position_20d": normalized["range_position_20d"],
        "return_20d_pct": normalized["return_20d_pct"],
        "return_5d_pct": normalized["return_5d_pct"],
    }
    weighted_terms = {
        name: round(value * MARKET_SCAN_RANK_REFINEMENT_WEIGHTS[name], MARKET_SCAN_RANK_REFINEMENT_SCORE_DECIMALS)
        for name, value in components.items()
    }
    score = round(sum(weighted_terms.values()), MARKET_SCAN_RANK_REFINEMENT_SCORE_DECIMALS)
    return MarketScanRankRefinement(
        raw_inputs=rounded_inputs,
        normalized_inputs=normalized,
        components=components,
        weighted_terms=weighted_terms,
        score=min(1.0, max(0.0, score)),
    )


def market_scan_rank_refinement_spec() -> dict[str, object]:
    return {
        "algorithm": MARKET_SCAN_RANK_REFINEMENT_ALGORITHM_VERSION,
        "normalization": "bounded-linear-0-1",
        "input_bounds": {name: list(bounds) for name, bounds in MARKET_SCAN_RANK_REFINEMENT_BOUNDS.items()},
        "components": {
            "ma_alignment": {
                "aggregation": "mean",
                "inputs": ["close_vs_ma5_pct", "ma5_vs_ma20_pct", "ma20_vs_ma60_pct"],
            },
            "range_position_20d": {"input": "range_position_20d"},
            "return_20d_pct": {"input": "return_20d_pct"},
            "return_5d_pct": {"input": "return_5d_pct"},
        },
        "weights": dict(MARKET_SCAN_RANK_REFINEMENT_WEIGHTS),
        "input_decimals": MARKET_SCAN_RANK_REFINEMENT_INPUT_DECIMALS,
        "score_decimals": MARKET_SCAN_RANK_REFINEMENT_SCORE_DECIMALS,
        "flat_range_fallback": 0.5,
        "max_rank_discount": MARKET_SCAN_RANK_REFINEMENT_MAX_DISCOUNT,
    }


def _pct_change(current: float, reference: float) -> float:
    return (current / reference - 1) * 100 if current > 0 and reference > 0 else 0.0


def _range_position(current: float, low: float, high: float) -> float:
    spread = high - low
    if spread <= 0:
        return 0.5
    return min(1.0, max(0.0, (current - low) / spread))


def _bounded_linear(value: float, lower: float, upper: float) -> float:
    if upper <= lower:
        raise ValueError("精排归一化边界无效")
    return round(min(1.0, max(0.0, (value - lower) / (upper - lower))), MARKET_SCAN_RANK_REFINEMENT_SCORE_DECIMALS)


__all__ = [
    "MARKET_SCAN_RANK_REFINEMENT_ALGORITHM_VERSION",
    "MARKET_SCAN_RANK_REFINEMENT_BOUNDS",
    "MARKET_SCAN_RANK_REFINEMENT_INPUT_DECIMALS",
    "MARKET_SCAN_RANK_REFINEMENT_MAX_DISCOUNT",
    "MARKET_SCAN_RANK_REFINEMENT_SCORE_DECIMALS",
    "MARKET_SCAN_RANK_REFINEMENT_WEIGHTS",
    "MarketScanRankRefinement",
    "market_scan_rank_refinement",
    "market_scan_rank_refinement_spec",
]
