"""Auditable, non-probabilistic score dimensions for full-market scan snapshots.

The production rank remains governed by ``full-market-score-v4``.  These
dimensions separate expected strength, confidence, risk and tradability so a
consumer does not have to interpret one ordinal rank as all four concepts.
They are persisted with the scan result and are therefore safe to use as
point-in-time research evidence later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
from statistics import fmean, pstdev
from typing import Mapping, Sequence, cast

from app.models.market import Kline, Quote
from app.models.market_scan import (
    MARKET_SCAN_MIN_HISTORY_ROWS,
    MarketScanMode,
    MarketScanResultItem,
)
from app.services.market_scan_feature_windows import (
    MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION,
    snapshot_return_pct,
    snapshot_skip_return_pct,
)
from app.services.market_scan_session_coverage import (
    MARKET_SCAN_SESSION_COVERAGE_CONTRACT_VERSION,
    MarketScanSessionCoverage,
    build_market_scan_session_coverage,
    verify_market_scan_session_coverage,
)
from app.utils.market_time import market_datetime_epoch


MARKET_SCAN_DIMENSION_SCHEMA_VERSION = 1
MARKET_SCAN_DIMENSION_ALGORITHM_VERSION = "full-market-dimensions-v4-session-coverage"
MARKET_SCAN_EVIDENCE_SCHEMA_VERSION = 1
MARKET_SCAN_EVIDENCE_CONTRACT_VERSION = (
    "market-scan-point-in-time-feature-evidence-v4-bar-as-of-bound"
)
MARKET_SCAN_EVIDENCE_LEGACY_V3_CONTRACT_VERSION = (
    "market-scan-point-in-time-feature-evidence-v3-score-and-identity-bound"
)
MARKET_SCAN_EVIDENCE_LEGACY_V2_CONTRACT_VERSION = (
    "market-scan-point-in-time-feature-evidence-v2"
)
MARKET_SCAN_EVIDENCE_LEGACY_CONTRACT_VERSION = "market-scan-point-in-time-feature-evidence-v1"
MARKET_SCAN_DIMENSION_DECIMALS = 4
_DIMENSION_SCORE_NAMES = (
    "alpha_1d",
    "alpha_5d",
    "alpha_20d",
    "confidence",
    "risk",
    "tradability",
)


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
                "return_windows": MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION,
                "session_coverage": MARKET_SCAN_SESSION_COVERAGE_CONTRACT_VERSION,
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


@dataclass(frozen=True)
class _DimensionBuildContext:
    item: MarketScanResultItem
    quote: Quote
    rows: Sequence[Kline]
    data_quality_score: int
    mode: MarketScanMode
    volume_context: dict[str, object]
    session_coverage: MarketScanSessionCoverage
    features: _DimensionFeatures


@dataclass(frozen=True)
class _DimensionScores:
    alpha_1d: float
    alpha_5d: float
    alpha_20d: float
    confidence: float
    risk: float
    tradability: float


@dataclass(frozen=True)
class _SnapshotReturns:
    one: float
    five: float
    twenty: float
    sixty: float
    skip5_twenty: float
    skip5_fifty_five: float


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
    context = _dimension_build_context(
        item,
        quote,
        rows,
        data_quality_score=data_quality_score,
        volume_ratio=volume_ratio,
        mode=mode,
    )
    return _dimension_result(context, _dimension_scores(context))


def _dimension_build_context(
    item: MarketScanResultItem,
    quote: Quote,
    rows: Sequence[Kline],
    *,
    data_quality_score: int,
    volume_ratio: float,
    mode: MarketScanMode,
) -> _DimensionBuildContext:
    volume_context = _volume_context(mode, rows)
    session_coverage = build_market_scan_session_coverage(rows)
    features = _dimension_features(
        quote,
        rows,
        volume_ratio,
        mode=mode,
        apply_volume_lifecycle=bool(volume_context["lifecycle_applied"]),
    )
    return _DimensionBuildContext(
        item,
        quote,
        rows,
        data_quality_score,
        mode,
        volume_context,
        session_coverage,
        features,
    )


def _dimension_scores(context: _DimensionBuildContext) -> _DimensionScores:
    item, quote, features = context.item, context.quote, context.features
    alpha_1d, alpha_5d, alpha_20d = _alpha_scores(context.features)
    confidence = _confidence_score(
        context.data_quality_score,
        quote_fallback=bool(quote.fallback_used),
        kline_fallback=any(row.fallback_used for row in context.rows),
        metadata_degraded=not str(item.industry or "").strip() or not str(item.list_date or "").strip(),
        history_count=MARKET_SCAN_MIN_HISTORY_ROWS,
        session_gap_penalty=context.session_coverage.confidence_penalty,
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
    return _DimensionScores(alpha_1d, alpha_5d, alpha_20d, confidence, risk, tradability)


def _dimension_result(
    context: _DimensionBuildContext,
    scores: _DimensionScores,
) -> MarketScanScoreDimensions:
    raw_features = _dimension_raw_features(context.features, context.quote)
    evidence = _point_in_time_evidence(
        context.item,
        context.quote,
        context.rows,
        context.data_quality_score,
        raw_features,
        scores,
        mode=context.mode,
        volume_context=context.volume_context,
        session_coverage=context.session_coverage,
    )
    return MarketScanScoreDimensions(
        alpha_1d=scores.alpha_1d,
        alpha_5d=scores.alpha_5d,
        alpha_20d=scores.alpha_20d,
        confidence=scores.confidence,
        risk=scores.risk,
        tradability=scores.tradability,
        decision_utility=_profile_utilities(*_score_tuple(scores)),
        raw_features=raw_features,
        volume_context=context.volume_context,
        evidence=evidence,
    )


def _score_tuple(scores: _DimensionScores) -> tuple[float, float, float, float, float, float]:
    return (
        scores.alpha_1d, scores.alpha_5d, scores.alpha_20d,
        scores.confidence, scores.risk, scores.tradability,
    )


def _score_dict(scores: _DimensionScores) -> dict[str, float]:
    return dict(zip(_DIMENSION_SCORE_NAMES, _score_tuple(scores), strict=True))


def _dimension_features(
    quote: Quote,
    rows: Sequence[Kline],
    volume_ratio: float,
    *,
    mode: MarketScanMode,
    apply_volume_lifecycle: bool,
) -> _DimensionFeatures:
    if len(rows) < MARKET_SCAN_MIN_HISTORY_ROWS:
        raise ValueError(f"多维评分至少需要{MARKET_SCAN_MIN_HISTORY_ROWS}根完整日K")
    return _dimension_features_from_values(
        float(quote.price),
        rows,
        volume_ratio,
        mode=mode,
        apply_volume_lifecycle=apply_volume_lifecycle,
    )


def _snapshot_returns(
    current: float,
    closes: Sequence[float],
    mode: MarketScanMode,
) -> _SnapshotReturns:
    return _SnapshotReturns(
        one=snapshot_return_pct(current, closes, horizon=1, mode=mode),
        five=snapshot_return_pct(current, closes, horizon=5, mode=mode),
        twenty=snapshot_return_pct(current, closes, horizon=20, mode=mode),
        sixty=snapshot_return_pct(current, closes, horizon=60, mode=mode),
        skip5_twenty=snapshot_skip_return_pct(
            closes, skip_sessions=5, lookback_sessions=20, mode=mode,
        ),
        skip5_fifty_five=snapshot_skip_return_pct(
            closes, skip_sessions=5, lookback_sessions=55, mode=mode,
        ),
    )


def _snapshot_range_position(current: float, rows: Sequence[Kline]) -> float:
    return _range_position(
        current,
        min(float(row.low) for row in rows[-20:]),
        max(float(row.high) for row in rows[-20:]),
    )


def _ma_alignment(current: float, ma5: float, ma20: float, ma60: float) -> float:
    return fmean(
        (
            _signed_unit(_pct_change(current, ma5), 5),
            _signed_unit(_pct_change(ma5, ma20), 8),
            _signed_unit(_pct_change(ma20, ma60), 15),
        )
    )


def _snapshot_volume_lifecycle(
    volume_ratio: float,
    returns: _SnapshotReturns,
    range20: float,
    enabled: bool,
) -> float:
    if not enabled:
        return 0.0
    return _volume_lifecycle_delta(
        volume_ratio=volume_ratio,
        return_1d=returns.one,
        return_5d=returns.five,
        range_position_20d=range20,
    )


def _volume_context(mode: MarketScanMode, rows: Sequence[Kline]) -> dict[str, object]:
    lifecycle_applied = mode in {"official", "preopen"}
    return {
        "mode": mode,
        "feature_window_contract": MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION,
        "snapshot_bar_position": (
            "previous-completed-session" if mode == "intraday" else "snapshot-session"
        ),
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
    return _raw_feature_values(
        features,
        amount=float(quote.amount),
        turnover_rate=float(quote.turnover_rate or 0.0),
    )


def _raw_feature_values(
    features: _DimensionFeatures,
    *,
    amount: float,
    turnover_rate: float,
) -> dict[str, float]:
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
            "amount": amount,
            "turnover_rate": turnover_rate,
        }
    )


def verify_market_scan_point_in_time_evidence(value: Mapping[str, object]) -> bool:
    """Verify an archived envelope.

    This digest-only entry point deliberately does not confer action
    eligibility.  Consumers making a decision must use the context-aware
    verifier below, which rejects legacy v1/v2 evidence and binds identity and
    derived scores to the persisted result row.
    """
    if not _valid_evidence_envelope(value):
        return False
    payload = value.get("payload")
    digest = value.get("payload_digest")
    if not isinstance(payload, dict) or not isinstance(digest, str):
        return False
    if digest != _stable_digest(payload):
        return False
    contract = value.get("contract_version")
    if contract == MARKET_SCAN_EVIDENCE_LEGACY_CONTRACT_VERSION:
        return True
    if contract == MARKET_SCAN_EVIDENCE_LEGACY_V2_CONTRACT_VERSION:
        return _verify_v2_evidence_payload(payload)
    if contract == MARKET_SCAN_EVIDENCE_LEGACY_V3_CONTRACT_VERSION:
        return _verify_legacy_v3_evidence_payload(value, payload)
    return _verify_current_evidence_payload(value, payload)


def verify_market_scan_point_in_time_evidence_context(
    value: Mapping[str, object],
    *,
    item: MarketScanResultItem,
    expected_data_date: str,
    expected_quote_date: str,
    expected_as_of: str,
    expected_mode: MarketScanMode | None = None,
    require_action_eligible: bool = True,
) -> bool:
    """Bind v4 evidence to one persisted result, score layer and run context."""
    if (
        value.get("contract_version") != MARKET_SCAN_EVIDENCE_CONTRACT_VERSION
        or not verify_market_scan_point_in_time_evidence(value)
        or require_action_eligible and value.get("action_eligible") is not True
    ):
        return False
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        return False
    if expected_mode is not None and payload.get("mode") != expected_mode:
        return False
    if not _evidence_time_context_matches(
        payload,
        item=item,
        expected_as_of=expected_as_of,
    ):
        return False
    if not _evidence_identity_matches(
        payload,
        item=item,
        expected_data_date=expected_data_date,
        expected_quote_date=expected_quote_date,
    ):
        return False
    outer_scores = _outer_dimension_scores(item)
    derived_scores = payload.get("derived_scores")
    if outer_scores is None or not isinstance(derived_scores, Mapping):
        return False
    if dict(derived_scores) != outer_scores:
        return False
    replayed = _replay_evidence_scores(payload)
    return replayed is not None and replayed == outer_scores


def market_scan_dimension_spec() -> dict[str, object]:
    """Canonical, hash-bound formula contract for all six research dimensions."""
    return {
        "schema_version": MARKET_SCAN_DIMENSION_SCHEMA_VERSION,
        "algorithm": MARKET_SCAN_DIMENSION_ALGORITHM_VERSION,
        "evidence_contract": MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
        "feature_window_contract": MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION,
        "session_coverage_contract": MARKET_SCAN_SESSION_COVERAGE_CONTRACT_VERSION,
        "score_decimals": MARKET_SCAN_DIMENSION_DECIMALS,
        "scores": list(_DIMENSION_SCORE_NAMES),
        "formula_contract": {
            "alpha_1d": "momentum+ma_alignment+volume_lifecycle-exhaustion",
            "alpha_5d": "momentum+ma_alignment+volume_lifecycle-exhaustion",
            "alpha_20d": "skip5_momentum+ma20_slope+return_60d",
            "confidence": "data_quality-fallback-metadata-history-session_gap_penalties",
            "risk": "atr+downside_volatility+drawdown+absolute_return+st+new",
            "tradability": "log_amount-turnover_extremes-limit_proximity-st",
        },
        "confidence": {
            "history_rows": MARKET_SCAN_MIN_HISTORY_ROWS,
            "fallback_penalties": {"quote": 8, "kline": 8, "metadata": 4},
            "history_penalty": "6 * unit(120-history_rows,0,60)",
            "session_gap_penalty": "sealed-session-coverage-confidence-penalty",
        },
        "semantics": {
            "ranking_effect": "none",
            "alpha": "ordinal-research-score-not-return-probability",
            "actionable": False,
        },
    }


def _valid_evidence_envelope(value: Mapping[str, object]) -> bool:
    supported_contracts = {
        MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
        MARKET_SCAN_EVIDENCE_LEGACY_V3_CONTRACT_VERSION,
        MARKET_SCAN_EVIDENCE_LEGACY_V2_CONTRACT_VERSION,
        MARKET_SCAN_EVIDENCE_LEGACY_CONTRACT_VERSION,
    }
    return (
        value.get("schema_version") == MARKET_SCAN_EVIDENCE_SCHEMA_VERSION
        and value.get("contract_version") in supported_contracts
        and value.get("status") == "verified-persisted-at-scan-time"
    )


def _verify_v2_evidence_payload(payload: Mapping[str, object]) -> bool:
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


def _verify_legacy_v3_evidence_payload(
    envelope: Mapping[str, object],
    payload: Mapping[str, object],
) -> bool:
    """Keep v3 readable for audit without granting current action eligibility."""
    if not _verify_v2_evidence_payload(payload):
        return False
    required = {
        "code",
        "name",
        "dimension_spec",
        "dimension_spec_hash",
        "derived_scores",
        "session_coverage",
        "quote_source",
        "kline_source",
        "adjustment_mode",
    }
    spec = payload.get("dimension_spec")
    scores = payload.get("derived_scores")
    coverage = payload.get("session_coverage")
    bars = payload.get("bar_contract_61")
    if (
        not required.issubset(payload)
        or not isinstance(spec, Mapping)
        or payload.get("dimension_spec_hash") != _stable_digest(spec)
        or not _valid_dimension_scores(scores)
        or not _valid_legacy_v3_bar_contract(bars)
        or not verify_market_scan_session_coverage(coverage, bar_contract=bars)
        or not isinstance(coverage, Mapping)
    ):
        return False
    action_eligible = coverage.get("action_eligible") is True
    return (
        envelope.get("action_eligible") is action_eligible
        and envelope.get("eligible_for_promotion_evidence") is action_eligible
    )


def _valid_legacy_v3_bar_contract(value: object) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, str | bytes)
        and len(value) == MARKET_SCAN_MIN_HISTORY_ROWS
        and all(
            isinstance(row, Sequence)
            and not isinstance(row, str | bytes)
            and len(row) == 9
            for row in value
        )
    )


def _verify_current_evidence_payload(
    envelope: Mapping[str, object],
    payload: Mapping[str, object],
) -> bool:
    if not _verify_v2_evidence_payload(payload):
        return False
    required = {
        "code",
        "name",
        "dimension_spec",
        "dimension_spec_hash",
        "derived_scores",
        "session_coverage",
        "quote_source",
        "kline_source",
        "adjustment_mode",
    }
    if not required.issubset(payload):
        return False
    spec = payload.get("dimension_spec")
    scores = payload.get("derived_scores")
    coverage = payload.get("session_coverage")
    if (
        not isinstance(spec, Mapping)
        or dict(spec) != market_scan_dimension_spec()
        or payload.get("dimension_spec_hash") != _stable_digest(spec)
        or not _valid_dimension_scores(scores)
        or not verify_market_scan_session_coverage(
            coverage,
            bar_contract=payload.get("bar_contract_61"),
        )
        or not isinstance(coverage, Mapping)
    ):
        return False
    action_eligible = coverage.get("action_eligible") is True
    return (
        envelope.get("action_eligible") is action_eligible
        and envelope.get("eligible_for_promotion_evidence") is action_eligible
    )


def _valid_volume_context(
    payload: Mapping[str, object],
    context: Mapping[str, object],
    mode: object,
) -> bool:
    envelope_valid = (
        context.get("mode") == mode
        and isinstance(context.get("lifecycle_applied"), bool)
        and context.get("volume_data_date") == payload.get("data_date")
    )
    return (
        envelope_valid
        and _valid_feature_window_context(context, mode)
        and _valid_volume_alignment(context, mode)
    )


def _valid_feature_window_context(
    context: Mapping[str, object],
    mode: object,
) -> bool:
    window_contract = context.get("feature_window_contract")
    if window_contract is None:
        return True
    expected_position = (
        "previous-completed-session" if mode == "intraday" else "snapshot-session"
    )
    return (
        window_contract == MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION
        and context.get("snapshot_bar_position") == expected_position
    )


def _valid_volume_alignment(context: Mapping[str, object], mode: object) -> bool:
    if mode == "intraday":
        return (
            context.get("lifecycle_applied") is False
            and context.get("price_volume_alignment")
            == "intraday-time-aligned-volume-unavailable-neutralized"
        )
    return (
        mode in {"official", "preopen"}
        and context.get("lifecycle_applied") is True
        and context.get("price_volume_alignment") == "same-completed-session"
    )


def _valid_dimension_scores(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == set(_DIMENSION_SCORE_NAMES)
        and all(_valid_score_value(item) for item in value.values())
    )


def _evidence_identity_matches(
    payload: Mapping[str, object],
    *,
    item: MarketScanResultItem,
    expected_data_date: str,
    expected_quote_date: str,
) -> bool:
    exact = {
        "symbol": item.symbol,
        "code": item.code,
        "market": item.market,
        "name": item.name,
        "industry": item.industry,
        "metadata_source": item.metadata_source,
        "list_date": item.list_date,
        "data_date": expected_data_date,
        "quote_date": expected_quote_date,
        "quote_timestamp": item.quote_timestamp,
        "quote_source": item.quote_source,
        "kline_source": item.kline_source,
        "adjustment_mode": item.adjustment_mode,
        "data_quality_score": item.data_quality_score,
        "is_st": item.is_st,
        "is_new": item.is_new,
        "quote_fallback_used": item.quote_fallback_used,
        "kline_fallback_used": item.kline_fallback_used,
        "metadata_degraded": item.metadata_degraded,
    }
    if item.data_date != expected_data_date or any(payload.get(key) != value for key, value in exact.items()):
        return False
    numeric = (
        ("quote_price", item.price, 8),
        ("quote_change_pct", item.change_pct, 8),
        ("quote_turnover_rate", item.turnover_rate, 8),
        ("quote_amount", item.amount, 4),
        ("reported_volume_ratio", item.volume_ratio, 8),
    )
    return all(_same_rounded_number(payload.get(key), value, decimals) for key, value, decimals in numeric)


def _evidence_time_context_matches(
    payload: Mapping[str, object],
    *,
    item: MarketScanResultItem,
    expected_as_of: str,
) -> bool:
    decision_epoch = market_datetime_epoch(expected_as_of)
    quote_event_epoch = market_datetime_epoch(item.quote_timestamp)
    quote_observed_epoch = market_datetime_epoch(item.quote_observed_at)
    if (
        decision_epoch is None
        or quote_event_epoch is None
        or quote_observed_epoch is None
        or quote_event_epoch > quote_observed_epoch
        or quote_observed_epoch > decision_epoch
    ):
        return False
    bars = payload.get("bar_contract_61")
    if not isinstance(bars, Sequence) or isinstance(bars, str | bytes):
        return False
    previous_snapshot_epoch: float | None = None
    for row in bars:
        if not isinstance(row, Sequence) or isinstance(row, str | bytes) or len(row) != 10:
            return False
        snapshot_epoch = _evidence_timestamp_epoch(row[9])
        try:
            row_date = date.fromisoformat(str(row[0]))
        except ValueError:
            return False
        row_start_epoch = market_datetime_epoch(
            f"{row_date.isoformat()} 00:00:00"
        )
        if (
            snapshot_epoch is None
            or row_start_epoch is None
            or snapshot_epoch < row_start_epoch
            or snapshot_epoch > decision_epoch
            or previous_snapshot_epoch is not None
            and snapshot_epoch < previous_snapshot_epoch
        ):
            return False
        previous_snapshot_epoch = snapshot_epoch
    return True


def _evidence_timestamp_epoch(value: object) -> float | None:
    text = str(value or "").strip()
    if len(text) == 10:
        text = f"{text} 00:00:00"
    return market_datetime_epoch(text)


def _outer_dimension_scores(item: MarketScanResultItem) -> dict[str, object] | None:
    components = item.score_details.get("components")
    if not isinstance(components, Mapping):
        return None
    dimensions = components.get("score_dimensions")
    if not isinstance(dimensions, Mapping):
        return None
    scores = dimensions.get("scores")
    if (
        not isinstance(scores, Mapping)
        or any(name not in scores or not _valid_score_value(scores[name]) for name in _DIMENSION_SCORE_NAMES)
    ):
        return None
    return {name: scores[name] for name in _DIMENSION_SCORE_NAMES}


def _replay_evidence_scores(payload: Mapping[str, object]) -> dict[str, object] | None:
    try:
        features, raw_features = _replay_evidence_features(payload)
        if payload.get("features") != raw_features:
            return None
        coverage = payload["session_coverage"]
        if not isinstance(coverage, Mapping):
            return None
        alpha_1d, alpha_5d, alpha_20d = _alpha_scores(features)
        confidence = _confidence_score(
            int(_finite_number(payload["data_quality_score"])),
            quote_fallback=bool(payload["quote_fallback_used"]),
            kline_fallback=bool(payload["kline_fallback_used"]),
            metadata_degraded=bool(payload["metadata_degraded"]),
            history_count=MARKET_SCAN_MIN_HISTORY_ROWS,
            session_gap_penalty=_finite_number(coverage["confidence_penalty"]),
        )
        risk = _risk_score(
            atr20_pct=features.atr20_pct,
            downside_volatility=features.downside_volatility,
            max_drawdown_60d=features.max_drawdown_60d,
            return_1d=features.return_1d,
            is_st=bool(payload["is_st"]),
            is_new=bool(payload["is_new"]),
        )
        tradability = _tradability_score(
            amount=_finite_number(payload["quote_amount"]),
            turnover_rate=_finite_number(payload["quote_turnover_rate"]),
            return_1d=features.return_1d,
            is_st=bool(payload["is_st"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    values = (alpha_1d, alpha_5d, alpha_20d, confidence, risk, tradability)
    return dict(zip(_DIMENSION_SCORE_NAMES, values, strict=True))


def _replay_evidence_features(
    payload: Mapping[str, object],
) -> tuple[_DimensionFeatures, dict[str, float]]:
    bars = _evidence_bars(payload.get("bar_contract_61"))
    mode = payload.get("mode")
    if mode not in {"official", "intraday", "preopen"}:
        raise ValueError("证据模式无效")
    current = _finite_number(payload["quote_price"])
    volume_ratio = _finite_number(payload["reported_volume_ratio"])
    context = payload.get("volume_context")
    if not isinstance(context, Mapping):
        raise ValueError("成交量上下文无效")
    features = _dimension_features_from_values(
        current,
        bars,
        volume_ratio,
        mode=cast(MarketScanMode, mode),
        apply_volume_lifecycle=context.get("lifecycle_applied") is True,
    )
    raw = _raw_feature_values(
        features,
        amount=_finite_number(payload["quote_amount"]),
        turnover_rate=_finite_number(payload["quote_turnover_rate"]),
    )
    return features, raw


def _evidence_bars(value: object) -> tuple[Kline, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError("证据日K格式无效")
    result: list[Kline] = []
    for row in value:
        if not isinstance(row, Sequence) or isinstance(row, str | bytes) or len(row) != 10:
            raise ValueError("证据日K行格式无效")
        result.append(
            Kline(
                date=str(row[0]),
                open=_finite_number(row[1]),
                close=_finite_number(row[2]),
                high=_finite_number(row[3]),
                low=_finite_number(row[4]),
                volume=_finite_number(row[5]),
                adjustment_mode=str(row[6]),
                data_version=str(row[7]),
                contract_version=str(row[8]),
                as_of=str(row[9]),
            )
        )
    if len(result) != MARKET_SCAN_MIN_HISTORY_ROWS:
        raise ValueError("证据必须恰好封存 61 根日K")
    return tuple(result)


def _dimension_features_from_values(
    current: float,
    rows: Sequence[Kline],
    volume_ratio: float,
    *,
    mode: MarketScanMode,
    apply_volume_lifecycle: bool,
) -> _DimensionFeatures:
    closes = [float(row.close) for row in rows]
    ma5, ma20, ma60 = fmean(closes[-5:]), fmean(closes[-20:]), fmean(closes[-60:])
    returns = _snapshot_returns(current, closes, mode)
    range20 = _snapshot_range_position(current, rows)
    return _DimensionFeatures(
        return_1d=returns.one,
        return_5d=returns.five,
        return_20d=returns.twenty,
        return_60d=returns.sixty,
        skip5_return_20d=returns.skip5_twenty,
        skip5_return_55d=returns.skip5_fifty_five,
        ma20_slope_10d=_pct_change(ma20, fmean(closes[-30:-10])),
        ma_alignment=_ma_alignment(current, ma5, ma20, ma60),
        atr20_pct=_atr(rows[-21:]) / current * 100 if current > 0 else 100.0,
        downside_volatility=_downside_volatility(closes[-21:]),
        max_drawdown_60d=abs(min(0.0, _maximum_drawdown(closes[-60:]))) * 100,
        range_position_20d=range20,
        volume_ratio=volume_ratio,
        lifecycle=_snapshot_volume_lifecycle(volume_ratio, returns, range20, apply_volume_lifecycle),
    )


def _point_in_time_evidence(
    item: MarketScanResultItem,
    quote: Quote,
    rows: Sequence[Kline],
    data_quality_score: int,
    raw_features: Mapping[str, float],
    scores: _DimensionScores,
    *,
    mode: MarketScanMode,
    volume_context: Mapping[str, object],
    session_coverage: MarketScanSessionCoverage,
) -> dict[str, object]:
    dimension_spec = market_scan_dimension_spec()
    coverage = session_coverage.as_dict()
    payload = _evidence_identity_payload(
        item,
        quote,
        rows,
        data_quality_score,
        raw_features,
        mode=mode,
        volume_context=volume_context,
    )
    payload.update(
        {
            "dimension_spec": dimension_spec,
            "dimension_spec_hash": _stable_digest(dimension_spec),
            "derived_scores": _score_dict(scores),
            "session_coverage": coverage,
        }
    )
    action_eligible = coverage["action_eligible"] is True
    return {
        "schema_version": MARKET_SCAN_EVIDENCE_SCHEMA_VERSION,
        "contract_version": MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
        "status": "verified-persisted-at-scan-time",
        "eligible_for_promotion_evidence": action_eligible,
        "action_eligible": action_eligible,
        "payload": payload,
        "payload_digest": _stable_digest(payload),
    }


def _evidence_bar_contract(rows: Sequence[Kline]) -> list[list[object]]:
    return [
        [
            row.date,
            float(row.open),
            float(row.close),
            float(row.high),
            float(row.low),
            float(row.volume),
            row.adjustment_mode,
            row.data_version,
            row.contract_version,
            row.as_of,
        ]
        for row in rows[-MARKET_SCAN_MIN_HISTORY_ROWS:]
    ]


def _evidence_identity_payload(
    item: MarketScanResultItem,
    quote: Quote,
    rows: Sequence[Kline],
    data_quality_score: int,
    raw_features: Mapping[str, float],
    *,
    mode: MarketScanMode,
    volume_context: Mapping[str, object],
) -> dict[str, object]:
    return {
        "symbol": item.symbol,
        "code": item.code,
        "market": item.market,
        "name": item.name,
        "industry": item.industry,
        "metadata_source": item.metadata_source,
        "quote_date": str(quote.timestamp or "")[:10],
        "data_date": rows[-1].date,
        "quote_timestamp": quote.timestamp,
        "quote_source": quote.source,
        "kline_source": rows[-1].source,
        "adjustment_mode": rows[-1].adjustment_mode,
        "quote_price": float(quote.price),
        "quote_change_pct": float(quote.change_pct),
        "quote_turnover_rate": (
            float(quote.turnover_rate) if quote.turnover_rate is not None else None
        ),
        "quote_amount": float(quote.amount),
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
        "bar_contract_61": _evidence_bar_contract(rows),
    }


def _confidence_score(
    quality: int,
    *,
    quote_fallback: bool,
    kline_fallback: bool,
    metadata_degraded: bool,
    history_count: int,
    session_gap_penalty: float,
) -> float:
    penalty = float(
        (8 if quote_fallback else 0)
        + (8 if kline_fallback else 0)
        + (4 if metadata_degraded else 0)
    )
    if history_count < 120:
        penalty += 6 * _unit(120 - history_count, 0, 60)
    penalty += session_gap_penalty
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


def _valid_score_value(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0 <= float(value) <= 100
    )


def _finite_number(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
    ):
        raise ValueError("证据数值必须有限")
    return float(value)


def _same_rounded_number(left: object, right: object, decimals: int) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return round(_finite_number(left), decimals) == round(_finite_number(right), decimals)
    except ValueError:
        return False


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
    "MARKET_SCAN_EVIDENCE_LEGACY_CONTRACT_VERSION",
    "MARKET_SCAN_EVIDENCE_LEGACY_V2_CONTRACT_VERSION",
    "MARKET_SCAN_EVIDENCE_LEGACY_V3_CONTRACT_VERSION",
    "MarketScanScoreDimensions",
    "build_market_scan_score_dimensions",
    "market_scan_dimension_spec",
    "verify_market_scan_point_in_time_evidence",
    "verify_market_scan_point_in_time_evidence_context",
]
