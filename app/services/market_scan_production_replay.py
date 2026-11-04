"""Pure v5 input replay from the observations already frozen in PIT v4.

Trend context follows the frozen subalgorithm ID, including legacy preopen
neutralization. Historical score versions retain downstream arithmetic replay.
Quality is bound by the PIT context verifier; its complete source assessment
was not frozen, so this module does not claim to reconstruct quality.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import cast

from app.models.market import Kline, Quote
from app.models.market_scan import MarketScanMode, MarketScanResultItem
from app.models.market_scan_execution_quote import (
    MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY,
    verify_market_scan_execution_quote_evidence,
)
from app.services.indicator_trend import trend_score
from app.services.indicator_trend_components import canonical_price_change_pct
from app.services.indicator_volume import recent_volume_ratio
from app.services.market_scan_rank_refinement import market_scan_rank_refinement
from app.services.market_scan_score_contract import (
    MARKET_SCAN_TREND_ALGORITHM_VERSION,
    market_scan_trend_context_mode,
    market_scan_shared_trend_algorithm,
)
from app.utils.market_time import market_datetime_epoch


def verify_market_scan_production_inputs(
    payload: Mapping[str, object],
    rows: Sequence[Kline],
    *,
    item: MarketScanResultItem,
) -> bool:
    """Bind v5 T, volume and continuous inputs to verified frozen OHLCV.

    The caller owns envelope, context, identity and bar validation.  The quote
    is constructed only from the observations consumed by its frozen version.
    The canonical-direction version also requires the recorded previous close;
    no unavailable quote fields are
    synthesized and no live providers, cache or quality assessment are used.
    """
    spec = item.score_details.get("score_spec")
    if not isinstance(spec, Mapping):
        return False
    if spec.get("schema_version") != 5:
        return spec.get("schema_version") in (2, 3, 4)
    inputs = item.score_details.get("inputs")
    if not isinstance(inputs, Mapping):
        return False
    mode = payload.get("mode")
    if mode not in ("official", "intraday", "preopen"):
        return False
    algorithms = spec.get("algorithms")
    if not isinstance(algorithms, Mapping):
        return False
    try:
        trend_algorithm = algorithms.get("trend_score")
        quote_inputs = {
            "price": _number(payload["quote_price"]),
            "change_pct": _number(payload["quote_change_pct"]),
            "turnover_rate": _number(payload["quote_turnover_rate"]),
        }
        if trend_algorithm == MARKET_SCAN_TREND_ALGORITHM_VERSION:
            quote_inputs["prev_close"] = _number(payload["quote_prev_close"])
            if not _canonical_quote_reference_matches(payload, quote_inputs, item):
                return False
        quote = Quote.model_construct(
            **quote_inputs,
        )
        expected = _expected_production_inputs(
            quote, list(rows), mode=cast(MarketScanMode, mode),
            trend_algorithm=trend_algorithm,
        )
        return (
            _number(item.trend_score) == expected["trend_score"]
            and _number(item.volume_ratio) == expected["volume_ratio"]
            and all(
                math.isclose(_number(inputs.get(name)), value, rel_tol=0.0, abs_tol=1e-8)
                for name, value in expected.items()
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def _canonical_quote_reference_matches(
    payload: Mapping[str, object], quote_inputs: Mapping[str, float], item: MarketScanResultItem,
) -> bool:
    """Bind the new direction reference to the separately frozen vendor quote."""
    evidence = verify_market_scan_execution_quote_evidence(
        item.score_details.get(MARKET_SCAN_EXECUTION_QUOTE_EVIDENCE_KEY),
    )
    bar = evidence["bar"]
    event_epoch = market_datetime_epoch(evidence["provider_event_at"])
    captured_epoch = market_datetime_epoch(evidence["captured_at"])
    quote_event_epoch = market_datetime_epoch(item.quote_timestamp)
    quote_observed_epoch = market_datetime_epoch(item.quote_observed_at)
    return (
        isinstance(bar, Mapping)
        and evidence["symbol"] == item.symbol
        and evidence["code"] == item.code
        and evidence["market"] == item.market
        and evidence["source"] == item.quote_source
        and evidence["fallback_used"] is item.quote_fallback_used
        and evidence["quote_date"] == payload.get("quote_date")
        and evidence["mode"] == payload.get("mode")
        and event_epoch is not None
        and captured_epoch is not None
        and quote_event_epoch is not None
        and quote_observed_epoch is not None
        and event_epoch == quote_event_epoch
        and captured_epoch == quote_observed_epoch
        and _number(bar["close"]) == quote_inputs["price"]
        and _number(bar["previous_close_reference"]) == quote_inputs["prev_close"]
        and _number(bar["amount"]) == _number(payload["quote_amount"])
    )


def _expected_production_inputs(
    quote: Quote, rows: list[Kline], *, mode: MarketScanMode, trend_algorithm: object,
) -> dict[str, float]:
    trend_mode = market_scan_trend_context_mode(mode, algorithm_version=trend_algorithm)
    trend, _ = trend_score(
        quote, rows, mode=trend_mode,
        algorithm_version=market_scan_shared_trend_algorithm(trend_algorithm),
    )
    volume_ratio = recent_volume_ratio(rows, recent_window=5, base_window=20)
    continuous = market_scan_rank_refinement(quote, rows, mode=mode)
    expected = {
        "trend_score": trend,
        "volume_ratio": volume_ratio,
        **{f"continuous_trend_{name}": value for name, value in continuous.raw_inputs.items()},
    }
    if trend_algorithm == MARKET_SCAN_TREND_ALGORITHM_VERSION:
        expected["scoring_change_pct"] = canonical_price_change_pct(quote.price, quote.prev_close)
    return expected


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("评分重放输入必须是有限数值")
    return float(value)


__all__ = ["verify_market_scan_production_inputs"]
