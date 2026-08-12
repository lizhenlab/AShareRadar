"""Auditable, non-probabilistic score dimensions for full-market scan snapshots.

The production rank remains governed by ``full-market-score-v4``.  These
dimensions separate expected strength, confidence, risk and tradability so a
consumer does not have to interpret one ordinal rank as all four concepts.
They are persisted with the scan result and are therefore safe to use as
point-in-time research evidence later.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from statistics import fmean, pstdev
from typing import Mapping, Sequence

from app.models.market import Kline, Quote
from app.models.market_scan import MarketScanMode, MarketScanResultItem


MARKET_SCAN_DIMENSION_SCHEMA_VERSION = 1
MARKET_SCAN_DIMENSION_ALGORITHM_VERSION = "full-market-dimensions-v2-time-aligned-volume"
MARKET_SCAN_EVIDENCE_SCHEMA_VERSION = 1
MARKET_SCAN_EVIDENCE_CONTRACT_VERSION = "market-scan-point-in-time-feature-evidence-v2"
MARKET_SCAN_EVIDENCE_LEGACY_CONTRACT_VERSION = "market-scan-point-in-time-feature-evidence-v1"
MARKET_SCAN_DIMENSION_DECIMALS = 4


@dataclass(frozen=True)
class MarketScanScoreDimensions:
    alpha_1d: float
    alpha_5d: float
    alpha_20d: float
    confidence: float
    risk: float
    tradability: float
    decision_utility: dict[str, float]
    raw_features: dict[str, float]
    volume_context: dict[str, object]
    evidence: dict[str, object]

    def details(self) -> dict[str, object]:
        return {
            "schema_version": MARKET_SCAN_DIMENSION_SCHEMA_VERSION,
            "algorithm": MARKET_SCAN_DIMENSION_ALGORITHM_VERSION,
            "semantics": {
                "alpha": "ordinal-research-score-not-return-probability",
                "confidence": "higher-is-more-reliable",
                "risk": "higher-is-riskier",
                "tradability": "higher-is-easier-to-execute",
                "decision_utility": "profile-specific-research-utility-not-advice",
                "volume": "completed-session-context; intraday lifecycle is neutral without time-aligned volume",
            },
            "scores": {
                "alpha_1d": self.alpha_1d,
                "alpha_5d": self.alpha_5d,
                "alpha_20d": self.alpha_20d,
                "confidence": self.confidence,
                "risk": self.risk,
                "tradability": self.tradability,
                "decision_utility": self.decision_utility,
            },
            "raw_features": self.raw_features,
            "volume_context": self.volume_context,
            "point_in_time_evidence": self.evidence,
        }


@dataclass(frozen=True)
class _DimensionFeatures:
    return_1d: float
    return_5d: float
    return_20d: float
    return_60d: float
    skip5_return_20d: float
    skip5_return_55d: float
    ma20_slope_10d: float
    ma_alignment: float
    atr20_pct: float
    downside_volatility: float
    max_drawdown_60d: float
    range_position_20d: float
    volume_ratio: float
    lifecycle: float


def build_market_scan_score_dimensions(
    item: MarketScanResultItem,
    quote: Quote,
    rows: Sequence[Kline],
    *,
    data_quality_score: int,
    volume_ratio: float,
    mode: MarketScanMode = "official",
) -> MarketScanScoreDimensions:
    """Build independent score dimensions and immutable feature evidence."""
    volume_context = _volume_context(mode, rows)
    apply_volume_lifecycle = bool(volume_context["lifecycle_applied"])
    features = _dimension_features(quote, rows, volume_ratio, apply_volume_lifecycle=apply_volume_lifecycle)
    alpha_1d, alpha_5d, alpha_20d = _alpha_scores(features)
    confidence = _confidence_score(
        data_quality_score,
        quote_fallback=bool(quote.fallback_used),
        kline_fallback=any(row.fallback_used for row in rows),
        metadata_degraded=not str(item.industry or "").strip() or not str(item.list_date or "").strip(),
        history_count=len(rows),
    )
    risk = _risk_score(
        atr20_pct=features.atr20_pct,
        downside_volatility=features.downside_volatility,
        max_drawdown_60d=features.max_drawdown_60d,
        return_1d=features.return_1d,
        is_st=item.is_st,
        is_new=item.is_new,
    )
    tradability = _tradability_score(
        amount=float(quote.amount),
        turnover_rate=float(quote.turnover_rate or 0.0),
        return_1d=features.return_1d,
        is_st=item.is_st,
    )
    utilities = _profile_utilities(alpha_1d, alpha_5d, alpha_20d, confidence, risk, tradability)
    raw_features = _dimension_raw_features(features, quote)
    evidence = _point_in_time_evidence(
        item,
        quote,
        rows,
        data_quality_score,
        raw_features,
        mode=mode,
        volume_context=volume_context,
    )
    return MarketScanScoreDimensions(
        alpha_1d=alpha_1d,
        alpha_5d=alpha_5d,
        alpha_20d=alpha_20d,
        confidence=confidence,
        risk=risk,
        tradability=tradability,
        decision_utility=utilities,
        raw_features=raw_features,
        volume_context=volume_context,
        evidence=evidence,
    )


def _dimension_features(
    quote: Quote,
    rows: Sequence[Kline],
    volume_ratio: float,
    *,
    apply_volume_lifecycle: bool,
) -> _DimensionFeatures:
    if len(rows) < 61:
        raise ValueError("多维评分至少需要61根完整日K")
    closes = [float(row.close) for row in rows]
    current = float(quote.price)
    ma5, ma20, ma60 = fmean(closes[-5:]), fmean(closes[-20:]), fmean(closes[-60:])
    return_1d = _pct_change(current, closes[-2])
    return_5d = _pct_change(current, closes[-6])
    range20 = _range_position(
        current,
        min(float(row.low) for row in rows[-20:]),
        max(float(row.high) for row in rows[-20:]),
    )
    alignment = fmean(
        (
            _signed_unit(_pct_change(current, ma5), 5),
            _signed_unit(_pct_change(ma5, ma20), 8),
            _signed_unit(_pct_change(ma20, ma60), 15),
        )
    )
    return _DimensionFeatures(
        return_1d=return_1d,
        return_5d=return_5d,
        return_20d=_pct_change(current, closes[-21]),
        return_60d=_pct_change(current, closes[-61]),
        skip5_return_20d=_pct_change(closes[-6], closes[-26]),
        skip5_return_55d=_pct_change(closes[-6], closes[-61]),
        ma20_slope_10d=_pct_change(ma20, fmean(closes[-30:-10])),
        ma_alignment=alignment,
        atr20_pct=_atr(rows[-21:]) / current * 100 if current > 0 else 100.0,
        downside_volatility=_downside_volatility(closes[-21:]),
        max_drawdown_60d=abs(min(0.0, _maximum_drawdown(closes[-60:]))) * 100,
        range_position_20d=range20,
        volume_ratio=volume_ratio,
        lifecycle=(
            _volume_lifecycle_delta(
                volume_ratio=volume_ratio,
                return_1d=return_1d,
                return_5d=return_5d,
                range_position_20d=range20,
            )
            if apply_volume_lifecycle
            else 0.0
        ),
    )


def _volume_context(mode: MarketScanMode, rows: Sequence[Kline]) -> dict[str, object]:
    lifecycle_applied = mode in {"official", "preopen"}
    return {
        "mode": mode,
        "volume_ratio_basis": "completed-daily-bars-5d-vs-20d",
        "volume_data_date": rows[-1].date,
        "price_volume_alignment": (
            "same-completed-session"
            if lifecycle_applied
            else "intraday-time-aligned-volume-unavailable-neutralized"
        ),
        "lifecycle_applied": lifecycle_applied,
    }


def _alpha_scores(features: _DimensionFeatures) -> tuple[float, float, float]:
    alpha_1d = _score(
        50 + 15 * _signed_unit(features.return_1d, 5) + 15 * features.ma_alignment
        + features.lifecycle - 12 * _unit(features.return_1d, 5, 10)
    )
    alpha_5d = _score(
        50 + 20 * _signed_unit(features.return_5d, 12) + 18 * features.ma_alignment
        + features.lifecycle - 10 * _unit(features.return_5d, 12, 25)
    )
    alpha_20d = _score(
        50 + 17 * _signed_unit(features.skip5_return_20d, 20)
        + 13 * _signed_unit(features.skip5_return_55d, 40)
        + 10 * _signed_unit(features.ma20_slope_10d, 10)
        + 4 * _signed_unit(features.return_60d, 50)
    )
    return alpha_1d, alpha_5d, alpha_20d


def _profile_utilities(
    alpha_1d: float,
    alpha_5d: float,
    alpha_20d: float,
    confidence: float,
    risk: float,
    tradability: float,
) -> dict[str, float]:
    return {
        "conservative": _utility(alpha_1d, alpha_5d, alpha_20d, confidence, risk, tradability, (0.10, 0.30, 0.60), 0.35),
        "balanced": _utility(alpha_1d, alpha_5d, alpha_20d, confidence, risk, tradability, (0.20, 0.35, 0.45), 0.25),
        "aggressive": _utility(alpha_1d, alpha_5d, alpha_20d, confidence, risk, tradability, (0.35, 0.40, 0.25), 0.15),
    }


def _dimension_raw_features(features: _DimensionFeatures, quote: Quote) -> dict[str, float]:
    return _rounded_features(
        {
            "return_1d_pct": features.return_1d,
            "return_5d_pct": features.return_5d,
            "return_20d_pct": features.return_20d,
            "return_60d_pct": features.return_60d,
            "skip5_return_20d_pct": features.skip5_return_20d,
            "skip5_return_55d_pct": features.skip5_return_55d,
            "ma20_slope_10d_pct": features.ma20_slope_10d,
            "ma_alignment": features.ma_alignment,
            "atr20_pct": features.atr20_pct,
            "downside_volatility_20d_pct": features.downside_volatility,
            "max_drawdown_60d_pct": features.max_drawdown_60d,
            "range_position_20d": features.range_position_20d,
            "volume_ratio": features.volume_ratio,
            "volume_lifecycle_delta": features.lifecycle,
            "amount": float(quote.amount),
            "turnover_rate": float(quote.turnover_rate or 0.0),
        }
    )


def verify_market_scan_point_in_time_evidence(value: Mapping[str, object]) -> bool:
    """Verify the self-contained feature evidence digest without market data."""
    if not _valid_evidence_envelope(value):
        return False
    payload = value.get("payload")
    digest = value.get("payload_digest")
    if not isinstance(payload, dict) or not isinstance(digest, str):
        return False
    if digest != _stable_digest(payload):
        return False
    if value.get("contract_version") == MARKET_SCAN_EVIDENCE_LEGACY_CONTRACT_VERSION:
        return True
    return _verify_current_evidence_payload(payload)


def _valid_evidence_envelope(value: Mapping[str, object]) -> bool:
    supported_contracts = {
        MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
        MARKET_SCAN_EVIDENCE_LEGACY_CONTRACT_VERSION,
    }
    return (
        value.get("schema_version") == MARKET_SCAN_EVIDENCE_SCHEMA_VERSION
        and value.get("contract_version") in supported_contracts
        and value.get("status") == "verified-persisted-at-scan-time"
    )


def _verify_current_evidence_payload(payload: Mapping[str, object]) -> bool:
    mode = payload.get("mode")
    context = payload.get("volume_context")
    if mode not in {"official", "intraday", "preopen"} or not isinstance(context, dict):
        return False
    required = {
        "symbol",
        "market",
        "industry",
        "quote_date",
        "data_date",
        "quote_timestamp",
        "quote_price",
        "quote_change_pct",
        "quote_turnover_rate",
        "quote_amount",
        "reported_volume_ratio",
        "data_quality_score",
        "is_st",
        "is_new",
        "quote_fallback_used",
        "kline_fallback_used",
        "metadata_degraded",
        "features",
        "bar_contract_61",
    }
    if not required.issubset(payload):
        return False
    return _valid_volume_context(payload, context, mode)


def _valid_volume_context(
    payload: Mapping[str, object],
    context: Mapping[str, object],
    mode: object,
) -> bool:
    if context.get("mode") != mode or not isinstance(context.get("lifecycle_applied"), bool):
        return False
    if context.get("volume_data_date") != payload.get("data_date"):
        return False
    if mode == "intraday" and (
        context.get("lifecycle_applied") is not False
        or context.get("price_volume_alignment") != "intraday-time-aligned-volume-unavailable-neutralized"
    ):
        return False
    if mode in {"official", "preopen"} and (
        context.get("lifecycle_applied") is not True
        or context.get("price_volume_alignment") != "same-completed-session"
    ):
        return False
    return True


def _point_in_time_evidence(
    item: MarketScanResultItem,
    quote: Quote,
    rows: Sequence[Kline],
    data_quality_score: int,
    raw_features: Mapping[str, float],
    *,
    mode: MarketScanMode,
    volume_context: Mapping[str, object],
) -> dict[str, object]:
    bar_contract = [
        [
            row.date,
            round(float(row.open), 8),
            round(float(row.close), 8),
            round(float(row.high), 8),
            round(float(row.low), 8),
            round(float(row.volume), 4),
            row.adjustment_mode,
            row.data_version,
            row.contract_version,
        ]
        for row in rows[-61:]
    ]
    payload: dict[str, object] = {
        "symbol": item.symbol,
        "market": item.market,
        "industry": item.industry,
        "metadata_source": item.metadata_source,
        "quote_date": str(quote.timestamp or "")[:10],
        "data_date": rows[-1].date,
        "quote_timestamp": quote.timestamp,
        "quote_price": round(float(quote.price), 8),
        "quote_change_pct": round(float(quote.change_pct), 8),
        "quote_turnover_rate": (
            round(float(quote.turnover_rate), 8) if quote.turnover_rate is not None else None
        ),
        "quote_amount": round(float(quote.amount), 4),
        "reported_volume_ratio": raw_features["volume_ratio"],
        "data_quality_score": int(data_quality_score),
        "mode": mode,
        "volume_context": dict(volume_context),
        "is_st": bool(item.is_st),
        "is_new": bool(item.is_new),
        "list_date": item.list_date,
        "quote_fallback_used": bool(quote.fallback_used),
        "kline_fallback_used": any(row.fallback_used for row in rows),
        "metadata_degraded": not str(item.industry or "").strip() or not str(item.list_date or "").strip(),
        "features": dict(raw_features),
        "bar_contract_61": bar_contract,
    }
    return {
        "schema_version": MARKET_SCAN_EVIDENCE_SCHEMA_VERSION,
        "contract_version": MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
        "status": "verified-persisted-at-scan-time",
        "eligible_for_promotion_evidence": True,
        "payload": payload,
        "payload_digest": _stable_digest(payload),
    }


def _confidence_score(
    quality: int,
    *,
    quote_fallback: bool,
    kline_fallback: bool,
    metadata_degraded: bool,
    history_count: int,
) -> float:
    penalty = float(
        (8 if quote_fallback else 0)
        + (8 if kline_fallback else 0)
        + (4 if metadata_degraded else 0)
    )
    if history_count < 120:
        penalty += 6 * _unit(120 - history_count, 0, 60)
    return _score(float(quality) - penalty)


def _risk_score(
    *,
    atr20_pct: float,
    downside_volatility: float,
    max_drawdown_60d: float,
    return_1d: float,
    is_st: bool,
    is_new: bool,
) -> float:
    return _score(
        10
        + 25 * _unit(atr20_pct, 2, 10)
        + 20 * _unit(downside_volatility, 1, 5)
        + 20 * _unit(max_drawdown_60d, 8, 35)
        + 10 * _unit(abs(return_1d), 5, 12)
        + (10 if is_st else 0)
        + (5 if is_new else 0)
    )


def _tradability_score(*, amount: float, turnover_rate: float, return_1d: float, is_st: bool) -> float:
    amount_score = 100 * _unit(math.log10(max(amount, 1.0)), math.log10(20_000_000), math.log10(1_000_000_000))
    turnover_penalty = 18 * _unit(0.5 - turnover_rate, 0, 0.5) + 18 * _unit(turnover_rate, 15, 35)
    limit_penalty = 25 * _unit(abs(return_1d), 7, 10)
    return _score(amount_score - turnover_penalty - limit_penalty - (10 if is_st else 0))


def _utility(
    alpha_1d: float,
    alpha_5d: float,
    alpha_20d: float,
    confidence: float,
    risk: float,
    tradability: float,
    weights: tuple[float, float, float],
    risk_weight: float,
) -> float:
    alpha = alpha_1d * weights[0] + alpha_5d * weights[1] + alpha_20d * weights[2]
    reliability_gate = 0.5 + 0.5 * min(confidence, tradability) / 100
    return _score(alpha * reliability_gate - risk_weight * risk)


def _volume_lifecycle_delta(
    *,
    volume_ratio: float,
    return_1d: float,
    return_5d: float,
    range_position_20d: float,
) -> float:
    confirmation = 8 * math.tanh(math.log(max(0.05, volume_ratio))) * _signed_unit(return_5d, 10)
    exhaustion = (
        10
        * _unit(volume_ratio, 2.0, 4.0)
        * max(_unit(return_1d, 5, 10), _unit(range_position_20d, 0.9, 1.0))
    )
    dry_up = 4 * _unit(0.8 - volume_ratio, 0, 0.5) * _unit(return_5d, -2, 5)
    return round(_clamp(confirmation - exhaustion + dry_up, -12, 8), MARKET_SCAN_DIMENSION_DECIMALS)


def _rounded_features(values: Mapping[str, float]) -> dict[str, float]:
    return {key: round(float(value), 8) for key, value in values.items()}


def _stable_digest(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _atr(rows: Sequence[Kline]) -> float:
    values = [
        max(
            float(row.high) - float(row.low),
            abs(float(row.high) - float(previous.close)),
            abs(float(row.low) - float(previous.close)),
        )
        for previous, row in zip(rows[:-1], rows[1:], strict=True)
    ]
    return fmean(values) if values else 0.0


def _downside_volatility(closes: Sequence[float]) -> float:
    returns = [current / previous - 1 for previous, current in zip(closes[:-1], closes[1:], strict=True) if previous > 0]
    downside = [value for value in returns if value < 0]
    return pstdev(downside) * 100 if len(downside) >= 2 else 0.0


def _maximum_drawdown(values: Sequence[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1 if peak > 0 else 0.0)
    return worst


def _range_position(value: float, low: float, high: float) -> float:
    return _clamp((value - low) / (high - low), 0, 1) if high > low else 0.5


def _pct_change(value: float, reference: float) -> float:
    return (value / reference - 1) * 100 if reference > 0 else 0.0


def _signed_unit(value: float, scale: float) -> float:
    return _clamp(value / scale, -1, 1) if scale > 0 else 0.0


def _unit(value: float, lower: float, upper: float) -> float:
    return _clamp((value - lower) / (upper - lower), 0, 1) if upper > lower else 0.0


def _score(value: float) -> float:
    return round(_clamp(value, 0, 100), MARKET_SCAN_DIMENSION_DECIMALS)


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


__all__ = [
    "MARKET_SCAN_DIMENSION_ALGORITHM_VERSION",
    "MARKET_SCAN_EVIDENCE_CONTRACT_VERSION",
    "MarketScanScoreDimensions",
    "build_market_scan_score_dimensions",
    "verify_market_scan_point_in_time_evidence",
]
