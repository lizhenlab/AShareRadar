"""Read-only, cross-sectional shadow scoring for full-market research.

This module is deliberately disconnected from the production scan write path.  It
turns an immutable published snapshot plus declared daily-bar inputs into a
replayable candidate ranking.  The caller must separately attest point-in-time
availability.  The candidate is evidence for later review; it is never a
production rule promotion mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
from statistics import fmean, pstdev
from typing import Literal, Mapping, Sequence, cast

from app.models.market import Kline
from app.models.market_scan import MARKET_SCAN_MIN_HISTORY_ROWS, MarketScanMode
from app.models.paper_trading import PaperInstrumentMetadata
from app.services.indicator_volume import recent_volume_ratio
from app.services.market_scan_feature_windows import (
    MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION,
    snapshot_return_pct,
    snapshot_skip_return_pct,
)
from app.services.paper_trading_rules import resolve_trade_rule_profile
from app.services.trading_calendar import is_trading_day
from app.utils.symbols import standard_symbol


SHADOW_SCORE_SCHEMA_VERSION = 4
SHADOW_SCORE_CANDIDATE_VERSION = "full-market-shadow-score-v5.3"
SHADOW_SCORE_ALGORITHM_VERSION = "residual-momentum-volume-lifecycle-v4"
SHADOW_SCORE_V54_SCHEMA_VERSION = 5
SHADOW_SCORE_V54_CANDIDATE_VERSION = "full-market-shadow-score-v5.4"
SHADOW_SCORE_V54_ALGORITHM_VERSION = "multilevel-residual-time-aligned-volume-risk-v1"
SHADOW_SCORE_V55_SCHEMA_VERSION = 6
SHADOW_SCORE_V55_CANDIDATE_VERSION = "full-market-shadow-score-v5.5"
SHADOW_SCORE_V55_ALGORITHM_VERSION = "bounded-gated-residual-stability-v1"
SHADOW_SCORE_RAW_DECIMALS = 6
SHADOW_SCORE_NOTIONAL = 100_000.0
SHADOW_SCORE_MIN_HISTORY_ROWS = MARKET_SCAN_MIN_HISTORY_ROWS
SHADOW_SCORE_MAX_OFFICIAL_CLOSE_GAP_PCT = 0.5
SHADOW_SCORE_MAX_OFFICIAL_CLOSE_GAP_ABSOLUTE = 0.02
SHADOW_SCORE_NORMALIZATION_PRIOR_COUNT = 30
SHADOW_SCORE_NORMALIZATION_MAX_BOARD_WEIGHT = 0.5
SHADOW_SCORE_VOLUME_RATIO_RECENT_WINDOW = 5
SHADOW_SCORE_VOLUME_RATIO_BASE_WINDOW = 20
SHADOW_SCORE_COMPONENT_REPLAY_TOLERANCE = 5e-7
ShadowScoreVariant = Literal[
    "v5_full",
    "v5_without_overextension",
    "v5_without_risk",
    "v5_without_liquidity",
    "v5_2_baseline",
    "v5_3_residual_momentum",
    "v5_3_skip5_residual_momentum",
    "v5_3_skip5_residual_volume_lifecycle",
    "v5_4_skip5_multilevel_residual",
    "v5_4_skip5_multilevel_residual_volume_lifecycle",
    "v5_5_bounded_nonlinear_stability",
]
SHADOW_SCORE_VARIANTS: tuple[ShadowScoreVariant, ...] = (
    "v5_full",
    "v5_without_overextension",
    "v5_without_risk",
    "v5_without_liquidity",
    "v5_2_baseline",
    "v5_3_residual_momentum",
    "v5_3_skip5_residual_momentum",
    "v5_3_skip5_residual_volume_lifecycle",
    "v5_4_skip5_multilevel_residual",
    "v5_4_skip5_multilevel_residual_volume_lifecycle",
    "v5_5_bounded_nonlinear_stability",
)
SHADOW_SCORE_RESIDUAL_VARIANTS: frozenset[ShadowScoreVariant] = frozenset(
    {
        "v5_3_residual_momentum",
        "v5_3_skip5_residual_momentum",
        "v5_3_skip5_residual_volume_lifecycle",
        "v5_4_skip5_multilevel_residual",
        "v5_4_skip5_multilevel_residual_volume_lifecycle",
        "v5_5_bounded_nonlinear_stability",
    }
)
SHADOW_SCORE_SKIP5_VARIANTS: frozenset[ShadowScoreVariant] = frozenset(
    {
        "v5_3_skip5_residual_momentum",
        "v5_3_skip5_residual_volume_lifecycle",
        "v5_4_skip5_multilevel_residual",
        "v5_4_skip5_multilevel_residual_volume_lifecycle",
        "v5_5_bounded_nonlinear_stability",
    }
)
SHADOW_SCORE_V54_VARIANTS: frozenset[ShadowScoreVariant] = frozenset(
    {
        "v5_4_skip5_multilevel_residual",
        "v5_4_skip5_multilevel_residual_volume_lifecycle",
    }
)
SHADOW_SCORE_V55_VARIANTS: frozenset[ShadowScoreVariant] = frozenset(
    {"v5_5_bounded_nonlinear_stability"}
)
SHADOW_SCORE_POINT_IN_TIME_VARIANTS = SHADOW_SCORE_V54_VARIANTS | SHADOW_SCORE_V55_VARIANTS
SHADOW_SCORE_MULTILEVEL_VARIANTS = SHADOW_SCORE_POINT_IN_TIME_VARIANTS


class ShadowScoreReplayError(ValueError):
    """Raised when persisted shadow evidence cannot be reproduced."""


@dataclass(frozen=True)
class ShadowScoreInput:
    symbol: str
    market: str
    quote_date: str
    data_date: str
    price: float
    change_pct: float
    turnover_rate: float | None
    amount: float
    volume_ratio: float
    data_quality_score: int
    rows: tuple[Kline, ...]
    list_date: str | None = None
    is_st: bool = False
    is_new: bool = False
    quote_fallback_used: bool = False
    kline_fallback_used: bool = False
    metadata_degraded: bool = False
    mode: MarketScanMode = "official"
    industry: str | None = None


@dataclass(frozen=True)
class ShadowScoreResult:
    symbol: str
    rank: int
    score: int
    raw_score: float
    variant: ShadowScoreVariant
    candidate_id: str
    spec_hash: str
    board: str
    details: dict[str, object]


@dataclass(frozen=True)
class ShadowScoreBatch:
    candidate_id: str
    variant: ShadowScoreVariant
    spec_hash: str
    spec: dict[str, object]
    normalization: dict[str, object]
    results: tuple[ShadowScoreResult, ...]


@dataclass(frozen=True)
class _RawFactors:
    item: ShadowScoreInput
    board: str
    trend_continuation: float
    skip5_momentum: float
    volume_confirmation_delta: float
    volume_lifecycle_delta: float
    overextension_penalty: float
    liquidity_penalty: float
    risk_penalty: float
    confidence_penalty: float
    special_status_penalty: float
    tradability_penalty: float
    constraint_flags: tuple[str, ...]
    inputs: dict[str, float | int | str | bool | None]


@dataclass(frozen=True)
class _PriceLimitContext:
    pct: float | None
    profile_id: str
    quality: str
    degradation_reasons: tuple[str, ...]


def score_shadow_market(
    items: Sequence[ShadowScoreInput],
    *,
    variant: ShadowScoreVariant = "v5_full",
) -> ShadowScoreBatch:
    """Score one complete snapshot without mutating production state."""
    if variant not in SHADOW_SCORE_VARIANTS:
        raise ValueError(f"未知影子评分消融版本：{variant}")
    if not items:
        raise ValueError("影子评分至少需要一只股票")
    symbols = [item.symbol for item in items]
    if len(symbols) != len(set(symbols)):
        raise ValueError("影子评分输入包含重复股票")
    if variant in SHADOW_SCORE_POINT_IN_TIME_VARIANTS:
        _validate_v54_temporal_context(items)

    raw = tuple(_raw_factors(item) for item in items)
    normalized, normalization = _normalize_candidate_factor(raw, variant)
    spec = market_scan_shadow_score_spec(variant=variant)
    spec_hash = stable_shadow_spec_hash(spec)
    candidate_id = f"{spec['candidate_version']}:{variant}:{spec_hash}"
    provisional = [
        _score_one(
            factors,
            normalized_trend=normalized[factors.item.symbol],
            variant=variant,
            candidate_id=candidate_id,
            spec=spec,
            spec_hash=spec_hash,
            normalization=normalization,
        )
        for factors in raw
    ]
    ordered = sorted(provisional, key=lambda item: (-item.raw_score, item.symbol))
    ranked = tuple(
        ShadowScoreResult(
            symbol=item.symbol,
            rank=index,
            score=item.score,
            raw_score=item.raw_score,
            variant=item.variant,
            candidate_id=item.candidate_id,
            spec_hash=item.spec_hash,
            board=item.board,
            details=item.details,
        )
        for index, item in enumerate(ordered, start=1)
    )
    batch = ShadowScoreBatch(
        candidate_id=candidate_id,
        variant=variant,
        spec_hash=spec_hash,
        spec=spec,
        normalization=normalization,
        results=ranked,
    )
    verify_shadow_score_batch(batch)
    return batch


def replay_shadow_score_details(details: Mapping[str, object]) -> float:
    """Recompute one candidate raw score from its persisted component evidence."""
    spec, components, inputs, expected_applied = _validated_shadow_replay_context(details)
    variant = _shadow_variant(spec.get("variant"))
    normalized_trend = _finite(components.get("normalized_trend"), "normalized_trend")
    volume_delta = _finite(components.get("volume_confirmation_delta"), "volume_confirmation_delta")
    _require_component_range(normalized_trend, 5.0, 95.0, "normalized_trend")
    volume_bounds = _shadow_volume_bounds(variant)
    _require_component_range(volume_delta, *volume_bounds, "volume_confirmation_delta")
    interaction_delta = _replayed_v55_interaction_delta(inputs, components, normalized_trend, variant)
    parsed_penalties = _validated_replay_penalties(components, variant)
    _verify_replayed_factor_components(inputs, components, volume_delta, parsed_penalties, variant)
    total_penalty = _applied_penalty_total(parsed_penalties, expected_applied)
    persisted_total = _finite(components.get("total_penalty"), "total_penalty")
    if not math.isclose(total_penalty, persisted_total, rel_tol=0, abs_tol=1e-8):
        raise ShadowScoreReplayError("影子评分总扣分无法重放")
    raw = round(
        _clamp(normalized_trend + volume_delta + interaction_delta - total_penalty, 0.0, 100.0),
        SHADOW_SCORE_RAW_DECIMALS,
    )
    persisted = _finite(components.get("raw_score"), "raw_score")
    if not math.isclose(raw, persisted, rel_tol=0, abs_tol=10 ** (-SHADOW_SCORE_RAW_DECIMALS)):
        raise ShadowScoreReplayError(f"影子评分重放不一致：{raw} != {persisted}")
    expected_score = max(0, min(100, round(raw)))
    if components.get("score") != expected_score:
        raise ShadowScoreReplayError("影子评分整数分无法重放")
    return raw


def _validated_shadow_replay_context(
    details: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object], Mapping[str, object], dict[str, bool]]:
    spec = _mapping(details.get("score_spec"), "score_spec")
    variant = _shadow_variant(spec.get("variant"))
    if details.get("schema_version") != _shadow_schema_version(variant):
        raise ShadowScoreReplayError("影子评分明细 schema 不受支持")
    if dict(spec) != market_scan_shadow_score_spec(variant=variant):
        raise ShadowScoreReplayError("影子评分规范不是已注册候选版本")
    if details.get("score_spec_hash") != stable_shadow_spec_hash(spec):
        raise ShadowScoreReplayError("影子评分规范哈希不一致")
    components = _mapping(details.get("components"), "components")
    applied = _mapping(components.get("applied_penalties"), "applied_penalties")
    component_spec = _mapping(spec.get("components"), "spec.components")
    names = ["overextension", "liquidity", "risk"]
    if variant in SHADOW_SCORE_POINT_IN_TIME_VARIANTS:
        names.append("tradability")
    expected_applied = {
        name: bool(_mapping(component_spec.get(f"{name}_penalty"), name).get("enabled"))
        for name in names
    }
    if dict(applied) != expected_applied:
        raise ShadowScoreReplayError("影子评分消融开关与规范不一致")
    return spec, components, _mapping(details.get("inputs"), "inputs"), expected_applied


def _validated_replay_penalties(
    components: Mapping[str, object],
    variant: ShadowScoreVariant,
) -> dict[str, float]:
    penalties = _mapping(components.get("penalties"), "penalties")
    parsed = {
        name: _finite(penalties.get(name), name)
        for name in ("overextension", "liquidity", "risk", "confidence", "special_status")
    }
    maxima = {"overextension": 20.0, "liquidity": 15.0, "risk": 15.0, "confidence": 20.0, "special_status": 8.0}
    if variant in SHADOW_SCORE_POINT_IN_TIME_VARIANTS:
        parsed["tradability"] = _finite(penalties.get("tradability"), "tradability")
        maxima["tradability"] = 20.0
    for name, maximum in maxima.items():
        _require_component_range(parsed[name], 0.0, maximum, name)
    return parsed


def _verify_replayed_factor_components(
    inputs: Mapping[str, object],
    components: Mapping[str, object],
    volume_delta: float,
    penalties: Mapping[str, float],
    variant: ShadowScoreVariant,
) -> None:
    expected_trend, expected_skip5, expected_base_volume, expected_lifecycle, expected_penalties = (
        _replay_factor_components(inputs, variant)
    )
    expected_volume_delta = _replayed_applied_volume_delta(
        inputs,
        variant,
        expected_base_volume,
        expected_lifecycle,
    )
    persisted_trend = _finite(components.get("raw_trend_continuation"), "raw_trend_continuation")
    if not _component_isclose(persisted_trend, expected_trend):
        raise ShadowScoreReplayError("影子评分趋势因子无法从输入重放")
    if not _component_isclose(volume_delta, expected_volume_delta):
        raise ShadowScoreReplayError("影子评分量能因子无法从输入重放")
    persisted_skip5 = _finite(components.get("raw_skip5_momentum"), "raw_skip5_momentum")
    if not _component_isclose(persisted_skip5, expected_skip5):
        raise ShadowScoreReplayError("影子评分跳过近5日动量无法从输入重放")
    expected_factor = expected_skip5 if variant in SHADOW_SCORE_SKIP5_VARIANTS else expected_trend
    if not _component_isclose(
        _finite(components.get("raw_normalization_factor"), "raw_normalization_factor"),
        expected_factor,
    ):
        raise ShadowScoreReplayError("影子评分归一化因子无法从输入重放")
    if any(not _component_isclose(penalties[name], expected) for name, expected in expected_penalties.items()):
        raise ShadowScoreReplayError("影子评分风险扣分无法从输入重放")
    if variant in SHADOW_SCORE_POINT_IN_TIME_VARIANTS:
        _verify_v54_replayed_context(inputs, components, volume_delta)


def _replayed_v55_interaction_delta(
    inputs: Mapping[str, object],
    components: Mapping[str, object],
    normalized_alpha: float,
    variant: ShadowScoreVariant,
) -> float:
    if variant not in SHADOW_SCORE_V55_VARIANTS:
        if "bounded_nonlinear_delta" in components or "challenger_evidence" in components:
            raise ShadowScoreReplayError("旧版影子评分包含未注册的 v5.5 交互证据")
        return 0.0
    persisted = _finite(components.get("bounded_nonlinear_delta"), "bounded_nonlinear_delta")
    _require_component_range(persisted, -6.0, 6.0, "bounded_nonlinear_delta")
    expected = _v55_challenger_evidence(inputs, normalized_alpha)
    if dict(_mapping(components.get("challenger_evidence"), "challenger_evidence")) != expected:
        raise ShadowScoreReplayError("v5.5 有界交互证据无法从冻结输入重放")
    if not _component_isclose(
        persisted,
        _finite(expected["bounded_nonlinear_delta"], "challenger_evidence.bounded_nonlinear_delta"),
    ):
        raise ShadowScoreReplayError("v5.5 有界交互增量无法重放")
    return persisted


def _verify_v54_replayed_context(
    inputs: Mapping[str, object],
    components: Mapping[str, object],
    volume_delta: float,
) -> None:
    constraint_evidence = _mapping(components.get("explicit_constraints"), "explicit_constraints")
    expected_flags = _replay_constraint_flags(inputs)
    if (
        constraint_evidence.get("flags") != expected_flags
        or constraint_evidence.get("clear") is not (not expected_flags)
    ):
        raise ShadowScoreReplayError("影子评分显式风险约束无法重放")
    volume_context = _mapping(components.get("volume_context"), "volume_context")
    expected_alignment = (
        "same-completed-session"
        if inputs.get("mode") in {"official", "preopen"}
        else "intraday-time-aligned-volume-unavailable-neutralized"
    )
    if (
        volume_context.get("mode") != inputs.get("mode")
        or volume_context.get("alignment") != expected_alignment
        or not _component_isclose(
            _finite(volume_context.get("applied_delta"), "volume_context.applied_delta"),
            volume_delta,
        )
    ):
        raise ShadowScoreReplayError("影子评分量能时点口径无法重放")


def _replay_constraint_flags(inputs: Mapping[str, object]) -> list[str]:
    flags: list[str] = []
    if _boolean(inputs.get("is_st"), "is_st"):
        flags.append("special_treatment")
    if _boolean(inputs.get("is_new"), "is_new"):
        flags.append("new_stock")
    if _finite(inputs.get("atr20_pct"), "atr20_pct") >= 8:
        flags.append("extreme_atr")
    if _finite(inputs.get("max_drawdown60_pct"), "max_drawdown60_pct") >= 35:
        flags.append("deep_drawdown")
    if _finite(inputs.get("notional_to_amount"), "notional_to_amount") > 0.01:
        flags.append("capacity_above_one_percent_of_amount")
    turnover = _finite(inputs.get("turnover_rate"), "turnover_rate")
    if turnover < 0.5 or turnover > 35:
        flags.append("turnover_extreme")
    limit_value = inputs.get("price_limit_pct")
    if limit_value is None:
        flags.append("price_limit_profile_unverified")
    else:
        limit_pct = _finite(limit_value, "price_limit_pct")
        if abs(_finite(inputs.get("derived_change_pct"), "derived_change_pct")) >= limit_pct * 0.9:
            flags.append("near_price_limit")
    return flags


def _applied_penalty_total(penalties: Mapping[str, float], applied: Mapping[str, bool]) -> float:
    return (
        (penalties["overextension"] if applied["overextension"] else 0.0)
        + (penalties["liquidity"] if applied["liquidity"] else 0.0)
        + (penalties["risk"] if applied["risk"] else 0.0)
        + penalties["confidence"]
        + penalties["special_status"]
        + (penalties.get("tradability", 0.0) if applied.get("tradability", False) else 0.0)
    )


def _shadow_volume_bounds(variant: ShadowScoreVariant) -> tuple[float, float]:
    if variant in {"v5_4_skip5_multilevel_residual", "v5_5_bounded_nonlinear_stability"}:
        return (0.0, 0.0)
    if variant in {
        "v5_3_skip5_residual_volume_lifecycle",
        "v5_4_skip5_multilevel_residual_volume_lifecycle",
    }:
        return (-12.0, 8.0)
    return (-6.0, 6.0)


def verify_shadow_score_batch(batch: ShadowScoreBatch) -> None:
    _verify_shadow_batch_identity(batch)
    replayed, raw_factor_rows = _replay_shadow_batch_results(batch)
    normalized, expected_normalization = _verify_shadow_batch_normalization(batch, raw_factor_rows)
    _verify_shadow_result_normalization(batch, normalized, expected_normalization)
    _verify_shadow_batch_ranking(batch, replayed)


def _verify_shadow_batch_identity(batch: ShadowScoreBatch) -> None:
    if batch.spec_hash != stable_shadow_spec_hash(batch.spec):
        raise ShadowScoreReplayError("影子评分批次规范哈希不一致")
    if batch.variant not in SHADOW_SCORE_VARIANTS or batch.spec != market_scan_shadow_score_spec(variant=batch.variant):
        raise ShadowScoreReplayError("影子评分批次规范不是已注册候选版本")
    expected_candidate_id = f"{batch.spec['candidate_version']}:{batch.variant}:{batch.spec_hash}"
    if batch.candidate_id != expected_candidate_id:
        raise ShadowScoreReplayError("影子评分批次候选标识不一致")
    if len({item.symbol for item in batch.results}) != len(batch.results):
        raise ShadowScoreReplayError("影子评分批次包含重复股票")


def _replay_shadow_batch_results(
    batch: ShadowScoreBatch,
) -> tuple[dict[str, float], list[tuple[str, str, float]]]:
    replayed: dict[str, float] = {}
    raw_factor_rows: list[tuple[str, str, float]] = []
    for item in batch.results:
        _verify_shadow_result_context(item, batch)
        replayed[item.symbol] = replay_shadow_score_details(item.details)
        components = _mapping(item.details.get("components"), "components")
        raw_factor_rows.append(
            (
                item.symbol,
                item.board,
                _finite(components.get("raw_normalization_factor"), "raw_normalization_factor"),
            )
        )
    return replayed, raw_factor_rows


def _verify_shadow_batch_normalization(
    batch: ShadowScoreBatch,
    raw_factor_rows: Sequence[tuple[str, str, float]],
) -> tuple[dict[str, float], dict[str, object]]:
    if batch.variant in SHADOW_SCORE_MULTILEVEL_VARIANTS:
        normalized, expected_normalization = _normalize_multilevel_residual_rows(
            _multilevel_replay_rows(batch)
        )
    elif batch.variant in SHADOW_SCORE_RESIDUAL_VARIANTS:
        normalized, expected_normalization = _normalize_residual_rows(raw_factor_rows)
    else:
        normalized, expected_normalization = _normalize_trend_rows(raw_factor_rows)
    if batch.normalization != expected_normalization:
        raise ShadowScoreReplayError("影子评分横截面归一化无法重放")
    return normalized, expected_normalization


def _verify_shadow_result_normalization(
    batch: ShadowScoreBatch,
    normalized: Mapping[str, float],
    expected_normalization: Mapping[str, object],
) -> None:
    for item in batch.results:
        components = _mapping(item.details.get("components"), "components")
        if not math.isclose(
            _finite(components.get("normalized_trend"), "normalized_trend"),
            normalized[item.symbol], rel_tol=0, abs_tol=1e-8,
        ):
            raise ShadowScoreReplayError("影子评分归一化分值无法重放")
        normalization = _mapping(item.details.get("normalization"), "details.normalization")
        if batch.variant in SHADOW_SCORE_MULTILEVEL_VARIANTS:
            inputs = _mapping(item.details.get("inputs"), "inputs")
            expected_detail = _multilevel_normalization_detail(expected_normalization, inputs)
        else:
            expected_groups = _mapping(expected_normalization.get("groups"), "normalization.groups")
            expected_detail = {
                "group": item.board,
                "group_summary": expected_groups[item.board],
                "batch_input_digest": expected_normalization["input_digest"],
            }
        if dict(normalization) != expected_detail:
            raise ShadowScoreReplayError("影子评分明细归一化上下文不一致")


def _multilevel_replay_rows(batch: ShadowScoreBatch) -> list[tuple[str, str, str, str, str, float]]:
    rows: list[tuple[str, str, str, str, str, float]] = []
    for item in batch.results:
        details = _mapping(item.details, "details")
        inputs = _mapping(details.get("inputs"), "inputs")
        components = _mapping(details.get("components"), "components")
        rows.append(
            (
                item.symbol,
                str(inputs.get("market") or "UNKNOWN"),
                item.board,
                str(inputs.get("industry") or "UNKNOWN"),
                str(inputs.get("liquidity_bucket") or "unknown"),
                _finite(components.get("raw_normalization_factor"), "raw_normalization_factor"),
            )
        )
    return rows


def _verify_shadow_batch_ranking(batch: ShadowScoreBatch, replayed: Mapping[str, float]) -> None:
    expected = sorted(batch.results, key=lambda item: (-replayed[item.symbol], item.symbol))
    if [item.symbol for item in expected] != [item.symbol for item in batch.results]:
        raise ShadowScoreReplayError("影子评分批次排名无法重放")
    if [item.rank for item in batch.results] != list(range(1, len(batch.results) + 1)):
        raise ShadowScoreReplayError("影子评分批次名次不连续")


def market_scan_shadow_score_spec(
    *,
    variant: ShadowScoreVariant = "v5_full",
) -> dict[str, object]:
    if variant not in SHADOW_SCORE_VARIANTS:
        raise ValueError(f"未知影子评分消融版本：{variant}")
    schema_version = _shadow_schema_version(variant)
    return {
        "schema_version": schema_version,
        "candidate_version": _shadow_candidate_version(variant),
        "algorithm": _shadow_algorithm_version(variant),
        "variant": variant,
        "purpose": "read-only-shadow-research-not-production",
        "inputs": _shadow_input_spec(variant),
        "normalization": _shadow_normalization_spec(variant),
        "components": _shadow_component_spec(_shadow_enabled_penalties(variant), variant),
        "final_score": _shadow_final_score_spec(variant),
        "promotion": "forbidden-without-independent-session-evidence",
    }


def _shadow_enabled_penalties(variant: ShadowScoreVariant) -> dict[str, bool]:
    if variant in SHADOW_SCORE_POINT_IN_TIME_VARIANTS:
        return {
            "overextension": True,
            "risk": True,
            "liquidity": False,
            "tradability": True,
        }
    return {
        "overextension": variant != "v5_without_overextension",
        "risk": variant != "v5_without_risk",
        "liquidity": variant != "v5_without_liquidity",
        "tradability": False,
    }


def _shadow_input_spec(variant: ShadowScoreVariant) -> dict[str, object]:
    spec: dict[str, object] = {
        "price_history": "qfq_daily_rows_not_after_data_date",
        "snapshot_quote": "persisted_market_scan_result",
        "minimum_history_rows": SHADOW_SCORE_MIN_HISTORY_ROWS,
        "price_window_contract": MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION,
        "return60_reference": "close_60_completed_sessions_before_snapshot_price",
        "change_pct_source": "derived_from_snapshot_price_and_previous_completed_close",
        "volume_ratio_source": "derived_from_validated-qfq-history",
        "intraday_volume_policy": "neutralize-without-time-aligned-intraday-volume",
        "volume_ratio_windows": {
            "recent": SHADOW_SCORE_VOLUME_RATIO_RECENT_WINDOW,
            "base": SHADOW_SCORE_VOLUME_RATIO_BASE_WINDOW,
        },
        "duplicate_bar_policy": "reject_conflicting-same-date-bars",
        "notional": SHADOW_SCORE_NOTIONAL,
    }
    if variant in SHADOW_SCORE_POINT_IN_TIME_VARIANTS:
        spec.update(
            {
                "scan_mode": "persisted-run-mode",
                "industry": "persisted-point-in-time-metadata-with-quality-gate",
                "intraday_volume_policy": "neutralize-without-time-aligned-intraday-volume",
            }
        )
    if variant in SHADOW_SCORE_V55_VARIANTS:
        spec["challenger_inputs"] = (
            "normalized_skip5_multilevel_residual_rank; skip5_return20/55; ma20_slope10; completed-volume-ratio; "
            "range_position20; turnover; notional_to_amount; frozen-confidence-penalty"
        )
        spec["challenger_limitations"] = [
            "industry_breadth_not_available_in_v55",
            "industry_effect_limited_to_existing_multilevel_residualization",
            "no_peer_network_or_holdings_inputs",
        ]
    return spec


def _shadow_normalization_spec(variant: ShadowScoreVariant) -> dict[str, object]:
    if variant in SHADOW_SCORE_MULTILEVEL_VARIANTS:
        return {
            "factor": "skip5_momentum",
            "method": "sequential-shrunk-market-board-industry-liquidity-residual-midrank-percentile",
            "group": "market-then-board-then-industry-then-liquidity",
            "output_range": [5.0, 95.0],
            "prior_count": SHADOW_SCORE_NORMALIZATION_PRIOR_COUNT,
            "maximum_group_weight": SHADOW_SCORE_NORMALIZATION_MAX_BOARD_WEIGHT,
            "industry_quality_policy": "unknown-or-broad-taxonomy-groups-are-not-neutralized",
            "blend": "subtract-shrunk-group-median-at-each-declared-step,then-global-midrank",
        }
    residual = variant in SHADOW_SCORE_RESIDUAL_VARIANTS
    return {
        "factor": "skip5_momentum" if variant in SHADOW_SCORE_SKIP5_VARIANTS else "trend_continuation",
        "method": (
            "market-board-centered-residual-midrank-percentile"
            if residual
            else "hierarchical-global-board-midrank-percentile-shrinkage"
        ),
        "group": "global-and-exchange-board",
        "output_range": [5.0, 95.0],
        "board_prior_count": SHADOW_SCORE_NORMALIZATION_PRIOR_COUNT,
        "maximum_board_weight": SHADOW_SCORE_NORMALIZATION_MAX_BOARD_WEIGHT,
        "blend": (
            "raw_factor-[global_median*(1-board_weight)+board_median*board_weight],then-global-midrank"
            if residual
            else "global_percentile*(1-board_weight)+board_percentile*board_weight"
        ),
    }


def _shadow_final_score_spec(variant: ShadowScoreVariant) -> dict[str, object]:
    if variant == "v5_5_bounded_nonlinear_stability":
        return {
            "formula": "normalized_alpha + bounded_nonlinear_delta - overextension-risk-confidence-special_status-tradability_penalties",
            "bounded_nonlinear_delta": [-6.0, 6.0],
            "volume_policy": "not-used-direct-v5.4-base",
            "training_claim": "none-deterministic-preregistered-shadow-challenger",
            "clamp": [0, 100],
            "raw_decimals": SHADOW_SCORE_RAW_DECIMALS,
            "tie_break": [["raw_score", "desc"], ["symbol", "asc"]],
        }
    if variant == "v5_4_skip5_multilevel_residual":
        return {
            "formula": "normalized_alpha - overextension-risk-confidence-special_status-tradability_penalties",
            "intraday_volume_policy": "not-used",
            "clamp": [0, 100],
            "raw_decimals": SHADOW_SCORE_RAW_DECIMALS,
            "tie_break": [["raw_score", "desc"], ["symbol", "asc"]],
        }
    if variant == "v5_4_skip5_multilevel_residual_volume_lifecycle":
        return {
            "formula": "normalized_alpha + time_aligned_volume_lifecycle_delta - overextension-risk-confidence-special_status-tradability_penalties",
            "intraday_volume_policy": "zero-unless-time-aligned-volume-is-persisted",
            "clamp": [0, 100],
            "raw_decimals": SHADOW_SCORE_RAW_DECIMALS,
            "tie_break": [["raw_score", "desc"], ["symbol", "asc"]],
        }
    volume_component = (
        "volume_lifecycle_delta"
        if variant == "v5_3_skip5_residual_volume_lifecycle"
        else "volume_confirmation_delta"
    )
    return {
        "formula": f"normalized_alpha + {volume_component} - enabled_penalties",
        "clamp": [0, 100],
        "raw_decimals": SHADOW_SCORE_RAW_DECIMALS,
        "tie_break": [["raw_score", "desc"], ["symbol", "asc"]],
    }


def _shadow_component_spec(
    enabled: Mapping[str, bool],
    variant: ShadowScoreVariant,
) -> dict[str, object]:
    components = _base_shadow_components(enabled)
    if enabled["tradability"]:
        components.update(_v54_shadow_components())
    if variant in SHADOW_SCORE_V55_VARIANTS:
        components.update(_v55_shadow_components())
    return components


def _base_shadow_components(enabled: Mapping[str, bool]) -> dict[str, object]:
    return {
        "trend_continuation": {
            "formula": "45% return20 + 35% return60 + 20% ma20_slope10",
            "raw_bounds": {"return20": [-15, 25], "return60": [-25, 50], "ma20_slope10": [-8, 12]},
            "role": "alpha",
        },
        "skip5_momentum": {
            "formula": "55% return20_excluding_latest5 + 35% return60_excluding_latest5 + 10% ma20_slope10",
            "role": "alpha-candidate",
        },
        "volume_confirmation_delta": {
            "bounds": [-6, 6],
            "volume_ratio_source": "validated-history-5d-vs-20d",
            "role": "signed-alpha-confirmation",
        },
        "volume_lifecycle_delta": {
            "bounds": [-12, 8],
            "inputs": ["derived_volume_ratio", "derived_change_pct", "return5_pct", "range_position20"],
            "role": "signed-alpha-confirmation-with-exhaustion",
        },
        "overextension_penalty": {
            "enabled": enabled["overextension"],
            "bounds": [0, 20],
            "inputs": ["positive_change_vs_limit", "price_vs_ma5_atr", "price_vs_ma20_atr", "range_position20"],
            "role": "penalty-only",
        },
        "liquidity_penalty": {
            "enabled": enabled["liquidity"],
            "bounds": [0, 15],
            "inputs": ["amount", "turnover_rate", "notional_to_amount"],
            "role": "penalty-only",
        },
        "risk_penalty": {
            "enabled": enabled["risk"],
            "bounds": [0, 15],
            "inputs": ["atr20_pct", "downside_volatility20", "max_drawdown60", "gap_frequency60"],
            "role": "penalty-only",
        },
        "confidence_penalty": {"bounds": [0, 20], "quality_policy": "penalty-only", "role": "penalty-only"},
        "special_status_penalty": {"st": 5, "new_stock": 3, "role": "explicit-segment-risk-penalty"},
    }


def _v54_shadow_components() -> dict[str, object]:
    return {
        "tradability_penalty": {
            "enabled": True,
            "bounds": [0, 20],
            "inputs": [
                "amount",
                "turnover_rate",
                "notional_to_amount",
                "positive_or_negative_price_limit_progress",
                "price_limit_quality",
            ],
            "role": "execution-and-capacity-penalty-only",
        },
        "explicit_constraints": {
            "inputs": [
                "is_st",
                "is_new",
                "atr20_pct",
                "max_drawdown60_pct",
                "price_limit_progress",
                "notional_to_amount",
            ],
            "role": "auditable-risk-flags-not-alpha",
        },
    }


def _v55_shadow_components() -> dict[str, object]:
    return {
        "bounded_nonlinear_interaction": {
            "role": "bounded-shadow-alpha-delta",
            "bounds": [-6.0, 6.0],
            "cross_sectional_residual_strength": "clamp((normalized_alpha-50)/45,-1,1)",
            "stability_gate": (
                "0.60*clamp(1-pstdev(short,medium,slope)/0.75,0,1)"
                "+0.40*abs(mean(short,medium,slope))"
            ),
            "implementability_gate": "clamp(1-0.55*crowding_risk-0.45*capacity_risk,0,1)",
            "formula": (
                "clamp(6*tanh(1.5*cross_sectional_residual_strength)*(0.35+0.65*stability_gate)"
                "*quality_gate*(implementability_gate if cross_sectional_residual_strength>0 else 1),-6,6)"
            ),
            "negative_residual_policy": (
                "implementability-gate-not-applied-to-negative-delta; conservative-downside-penalty"
            ),
            "limitations": [
                "industry_breadth_not_available_in_v55",
                "industry_effect_limited_to_existing_multilevel_residualization",
                "no_peer_network_or_holdings_inputs",
            ],
            "training_claim": "none-deterministic-paper-inspired-interaction",
        },
        "crowding_risk": {
            "bounds": [0.0, 1.0],
            "formula": "0.45*turnover_extreme+0.35*completed_volume_surge+0.20*range_crowding",
            "role": "positive-alpha-gate-only",
        },
        "capacity_risk": {
            "bounds": [0.0, 1.0],
            "formula": "unit(notional_to_amount,0.2%,1.0%)",
            "role": "positive-alpha-gate-only",
        },
    }


def _shadow_schema_version(variant: ShadowScoreVariant) -> int:
    if variant in SHADOW_SCORE_V55_VARIANTS:
        return SHADOW_SCORE_V55_SCHEMA_VERSION
    return SHADOW_SCORE_V54_SCHEMA_VERSION if variant in SHADOW_SCORE_V54_VARIANTS else SHADOW_SCORE_SCHEMA_VERSION


def _shadow_candidate_version(variant: ShadowScoreVariant) -> str:
    if variant in SHADOW_SCORE_V55_VARIANTS:
        return SHADOW_SCORE_V55_CANDIDATE_VERSION
    return SHADOW_SCORE_V54_CANDIDATE_VERSION if variant in SHADOW_SCORE_V54_VARIANTS else SHADOW_SCORE_CANDIDATE_VERSION


def _shadow_algorithm_version(variant: ShadowScoreVariant) -> str:
    if variant in SHADOW_SCORE_V55_VARIANTS:
        return SHADOW_SCORE_V55_ALGORITHM_VERSION
    return SHADOW_SCORE_V54_ALGORITHM_VERSION if variant in SHADOW_SCORE_V54_VARIANTS else SHADOW_SCORE_ALGORITHM_VERSION


def stable_shadow_spec_hash(spec: Mapping[str, object]) -> str:
    payload = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _raw_factors(item: ShadowScoreInput) -> _RawFactors:
    rows = _validated_history(item)
    closes = [float(row.close) for row in rows]
    current = float(item.price)
    ma5, ma20, ma60 = fmean(closes[-5:]), fmean(closes[-20:]), fmean(closes[-60:])
    derived_change_pct = _snapshot_change_pct(item, rows, current)
    momentum = _momentum_components(closes, current, ma20, mode=item.mode)
    return20, return60, skip5_return20, skip5_return55 = momentum[:4]
    ma20_slope10, trend, skip5_momentum, return5 = momentum[4:]
    atr20 = _atr(rows[-21:])
    atr_pct = atr20 / current * 100 if current > 0 else 100.0
    range20 = _range_position(current, min(row.low for row in rows[-20:]), max(row.high for row in rows[-20:]))
    board = _board(item.symbol, item.market)
    limit_context = _price_limit_context(item)
    ma5_atr = max(0.0, current - ma5) / atr20 if atr20 > 0 else 0.0
    ma20_atr = max(0.0, current - ma20) / atr20 if atr20 > 0 else 0.0
    overextension = _overextension_penalty(derived_change_pct, limit_context.pct, ma5_atr, ma20_atr, range20)
    derived_volume_ratio, volume_delta, volume_lifecycle_delta = _volume_components(
        rows, derived_change_pct, return5, range20,
    )
    liquidity_penalty = _liquidity_penalty(item.amount, item.turnover_rate)
    risk, downside_vol, drawdown, gap_frequency = _risk_penalty(rows, closes, atr_pct)
    confidence = _confidence_penalty(item, len(rows))
    special = (5.0 if item.is_st else 0.0) + (3.0 if item.is_new else 0.0)
    tradability = _tradability_penalty(
        amount=item.amount,
        turnover_rate=float(item.turnover_rate or 0.0),
        change_pct=derived_change_pct,
        limit_pct=limit_context.pct,
        price_limit_quality=limit_context.quality,
    )
    constraints = _constraint_flags(
        item=item,
        atr_pct=atr_pct,
        drawdown=drawdown,
        derived_change_pct=derived_change_pct,
        limit_context=limit_context,
    )
    return _RawFactors(
        item=item,
        board=board,
        trend_continuation=round(trend, 8),
        skip5_momentum=round(skip5_momentum, 8),
        volume_confirmation_delta=round(volume_delta, 8),
        volume_lifecycle_delta=round(volume_lifecycle_delta, 8),
        overextension_penalty=round(overextension, 8),
        liquidity_penalty=round(liquidity_penalty, 8),
        risk_penalty=round(risk, 8),
        confidence_penalty=round(confidence, 8),
        special_status_penalty=round(special, 8),
        tradability_penalty=round(tradability, 8),
        constraint_flags=constraints,
        inputs=_factor_inputs(
            item, board, len(rows), return20, return60, skip5_return20, skip5_return55,
            return5, ma20_slope10, ma5, ma20, ma60,
            atr_pct, ma5_atr, ma20_atr, range20, downside_vol, drawdown, gap_frequency,
            derived_change_pct, derived_volume_ratio, limit_context,
        ),
    )


def _momentum_components(
    closes: Sequence[float],
    current: float,
    ma20: float,
    *,
    mode: MarketScanMode,
) -> tuple[float, float, float, float, float, float, float, float]:
    return20 = snapshot_return_pct(current, closes, horizon=20, mode=mode)
    return60 = snapshot_return_pct(current, closes, horizon=60, mode=mode)
    skip5_return20 = snapshot_skip_return_pct(
        closes,
        skip_sessions=5,
        lookback_sessions=20,
        mode=mode,
    )
    skip5_return55 = snapshot_skip_return_pct(
        closes,
        skip_sessions=5,
        lookback_sessions=55,
        mode=mode,
    )
    slope = _pct_change(ma20, fmean(closes[-30:-10]))
    trend = 100 * (
        0.45 * _unit(return20, -15, 25)
        + 0.35 * _unit(return60, -25, 50)
        + 0.20 * _unit(slope, -8, 12)
    )
    skip5 = 100 * (
        0.55 * _unit(skip5_return20, -15, 25)
        + 0.35 * _unit(skip5_return55, -25, 50)
        + 0.10 * _unit(slope, -8, 12)
    )
    return (
        return20,
        return60,
        skip5_return20,
        skip5_return55,
        slope,
        trend,
        skip5,
        snapshot_return_pct(current, closes, horizon=5, mode=mode),
    )


def _volume_components(
    rows: Sequence[Kline],
    change_pct: float,
    return5: float,
    range20: float,
) -> tuple[float, float, float]:
    volume_ratio = recent_volume_ratio(
        list(rows),
        recent_window=SHADOW_SCORE_VOLUME_RATIO_RECENT_WINDOW,
        base_window=SHADOW_SCORE_VOLUME_RATIO_BASE_WINDOW,
    )
    direction = _clamp(change_pct / 3.0, -1.0, 1.0)
    delta = _clamp(6 * math.tanh(math.log(max(0.05, volume_ratio))) * direction, -6, 6)
    return volume_ratio, delta, _volume_lifecycle_delta(volume_ratio, change_pct, return5, range20)


def _volume_lifecycle_delta(
    volume_ratio: float,
    change_pct: float,
    return5: float,
    range20: float,
) -> float:
    confirmation = 8 * math.tanh(math.log(max(0.05, volume_ratio))) * _clamp(return5 / 10, -1, 1)
    exhaustion = 10 * _unit(volume_ratio, 2, 4) * max(
        _unit(change_pct, 5, 10),
        _unit(range20, 0.90, 1.0),
    )
    dry_up = 4 * _unit(0.8 - volume_ratio, 0, 0.5) * _unit(return5, -2, 5)
    return _clamp(confirmation - exhaustion + dry_up, -12, 8)


def _overextension_penalty(
    change_pct: float,
    limit_pct: float | None,
    ma5_atr: float,
    ma20_atr: float,
    range20: float,
) -> float:
    limit_progress = (
        _unit(max(0.0, change_pct), limit_pct * 0.55, limit_pct * 0.98)
        if limit_pct is not None and limit_pct > 0
        else 0.0
    )
    return _clamp(
        8 * limit_progress
        + 5 * _unit(ma5_atr, 1.0, 3.5)
        + 4 * _unit(ma20_atr, 2.0, 6.0)
        + 3 * _unit(range20, 0.85, 1.0),
        0,
        20,
    )


def _risk_penalty(
    rows: Sequence[Kline],
    closes: Sequence[float],
    atr_pct: float,
) -> tuple[float, float, float, float]:
    returns = [_return(closes[index], closes[index - 1]) for index in range(1, len(closes))]
    downside = [value for value in returns[-20:] if value < 0]
    downside_vol = pstdev(downside) * 100 if len(downside) >= 2 else 0.0
    drawdown = abs(min(0.0, _max_drawdown(closes[-60:]))) * 100
    gap_rows = rows[-SHADOW_SCORE_MIN_HISTORY_ROWS:]
    gaps = [abs(_return(float(row.open), float(previous.close))) for previous, row in zip(gap_rows[:-1], gap_rows[1:], strict=True)]
    gap_frequency = sum(value >= 0.03 for value in gaps) / len(gaps) if gaps else 0.0
    risk = _risk_penalty_from_metrics(atr_pct, downside_vol, drawdown, gap_frequency)
    return risk, downside_vol, drawdown, gap_frequency


def _risk_penalty_from_metrics(
    atr_pct: float,
    downside_vol: float,
    drawdown: float,
    gap_frequency: float,
) -> float:
    return _clamp(
        5 * _unit(atr_pct, 2, 10)
        + 4 * _unit(downside_vol, 1, 5)
        + 4 * _unit(drawdown, 8, 35)
        + 2 * _unit(gap_frequency, 0.02, 0.20),
        0,
        15,
    )


def _tradability_penalty(
    *,
    amount: float,
    turnover_rate: float,
    change_pct: float,
    limit_pct: float | None,
    price_limit_quality: str,
) -> float:
    amount_penalty = 8 * _unit(math.log10(100_000_000) - math.log10(max(amount, 1.0)), 0, 1)
    participation = SHADOW_SCORE_NOTIONAL / max(amount, 1.0)
    capacity_penalty = 6 * _unit(participation, 0.002, 0.01)
    turnover_penalty = 3 * _unit(0.5 - turnover_rate, 0, 0.5) + 3 * _unit(turnover_rate, 15, 35)
    limit_progress = abs(change_pct) / limit_pct if limit_pct is not None and limit_pct > 0 else 0.0
    limit_penalty = 8 * _unit(limit_progress, 0.80, 0.99)
    profile_penalty = 2.0 if price_limit_quality != "verified" else 0.0
    return _clamp(
        amount_penalty + capacity_penalty + turnover_penalty + limit_penalty + profile_penalty,
        0,
        20,
    )


def _constraint_flags(
    *,
    item: ShadowScoreInput,
    atr_pct: float,
    drawdown: float,
    derived_change_pct: float,
    limit_context: _PriceLimitContext,
) -> tuple[str, ...]:
    flags: list[str] = []
    if item.is_st:
        flags.append("special_treatment")
    if item.is_new:
        flags.append("new_stock")
    if atr_pct >= 8:
        flags.append("extreme_atr")
    if drawdown >= 35:
        flags.append("deep_drawdown")
    if SHADOW_SCORE_NOTIONAL / max(item.amount, 1.0) > 0.01:
        flags.append("capacity_above_one_percent_of_amount")
    if item.turnover_rate is not None and (item.turnover_rate < 0.5 or item.turnover_rate > 35):
        flags.append("turnover_extreme")
    if limit_context.pct is None:
        flags.append("price_limit_profile_unverified")
    elif abs(derived_change_pct) >= limit_context.pct * 0.9:
        flags.append("near_price_limit")
    return tuple(flags)


def _confidence_penalty(item: ShadowScoreInput, history_rows: int) -> float:
    penalty = _clamp((100 - item.data_quality_score) * 0.15, 0, 15)
    penalty += 1.5 if item.quote_fallback_used else 0.0
    penalty += 1.5 if item.kline_fallback_used else 0.0
    penalty += 1.0 if item.metadata_degraded else 0.0
    penalty += 2.0 * _unit(120 - history_rows, 0, 60) if history_rows < 120 else 0.0
    return _clamp(penalty, 0, 20)


def _replay_factor_components(
    inputs: Mapping[str, object],
    variant: ShadowScoreVariant,
) -> tuple[float, float, float, float, dict[str, float]]:
    if inputs.get("price_window_contract") != MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION:
        raise ShadowScoreReplayError("影子评分价格窗口契约不一致")
    trend, skip5 = _replay_momentum_components(inputs)
    volume_delta, lifecycle = _replay_volume_components(inputs)
    return trend, skip5, volume_delta, lifecycle, _replay_penalties(inputs, variant)


def _replayed_applied_volume_delta(
    inputs: Mapping[str, object],
    variant: ShadowScoreVariant,
    base_volume_delta: float,
    lifecycle_delta: float,
) -> float:
    if inputs.get("mode") == "intraday":
        return 0.0
    if variant in {"v5_4_skip5_multilevel_residual", "v5_5_bounded_nonlinear_stability"}:
        return 0.0
    if variant == "v5_4_skip5_multilevel_residual_volume_lifecycle":
        return lifecycle_delta if inputs.get("mode") in {"official", "preopen"} else 0.0
    if variant == "v5_3_skip5_residual_volume_lifecycle":
        return lifecycle_delta
    return base_volume_delta


def _replay_momentum_components(inputs: Mapping[str, object]) -> tuple[float, float]:
    return20 = _finite(inputs.get("return20_pct"), "return20_pct")
    return60 = _finite(inputs.get("return60_pct"), "return60_pct")
    slope = _finite(inputs.get("ma20_slope10_pct"), "ma20_slope10_pct")
    trend = round(
        100
        * (
            0.45 * _unit(return20, -15, 25)
            + 0.35 * _unit(return60, -25, 50)
            + 0.20 * _unit(slope, -8, 12)
        ),
        8,
    )
    skip5 = round(
        100
        * (
            0.55 * _unit(_finite(inputs.get("skip5_return20_pct"), "skip5_return20_pct"), -15, 25)
            + 0.35 * _unit(_finite(inputs.get("skip5_return55_pct"), "skip5_return55_pct"), -25, 50)
            + 0.10 * _unit(slope, -8, 12)
        ),
        8,
    )
    return trend, skip5


def _replay_volume_components(inputs: Mapping[str, object]) -> tuple[float, float]:
    change_pct = _finite(inputs.get("derived_change_pct"), "derived_change_pct")
    volume_ratio = _finite(inputs.get("derived_volume_ratio"), "derived_volume_ratio")
    direction = _clamp(change_pct / 3.0, -1.0, 1.0)
    volume_delta = round(
        _clamp(6 * math.tanh(math.log(max(0.05, volume_ratio))) * direction, -6, 6),
        8,
    )
    lifecycle = round(
        _volume_lifecycle_delta(
            volume_ratio,
            change_pct,
            _finite(inputs.get("return5_pct"), "return5_pct"),
            _finite(inputs.get("range_position20"), "range_position20"),
        ),
        8,
    )
    return volume_delta, lifecycle


def _replay_penalties(
    inputs: Mapping[str, object],
    variant: ShadowScoreVariant,
) -> dict[str, float]:
    change_pct = _finite(inputs.get("derived_change_pct"), "derived_change_pct")
    limit_value = inputs.get("price_limit_pct")
    limit_pct = None if limit_value is None else _finite(limit_value, "price_limit_pct")
    overextension = _overextension_penalty(
        change_pct,
        limit_pct,
        _finite(inputs.get("price_vs_ma5_atr"), "price_vs_ma5_atr"),
        _finite(inputs.get("price_vs_ma20_atr"), "price_vs_ma20_atr"),
        _finite(inputs.get("range_position20"), "range_position20"),
    )
    liquidity = _liquidity_penalty(
        _finite(inputs.get("amount"), "amount"),
        _finite(inputs.get("turnover_rate"), "turnover_rate"),
    )
    risk = _risk_penalty_from_metrics(
        _finite(inputs.get("atr20_pct"), "atr20_pct"),
        _finite(inputs.get("downside_volatility20_pct"), "downside_volatility20_pct"),
        _finite(inputs.get("max_drawdown60_pct"), "max_drawdown60_pct"),
        _finite(inputs.get("gap_frequency60"), "gap_frequency60"),
    )
    quality = _finite(inputs.get("data_quality_score"), "data_quality_score")
    history_rows = _finite(inputs.get("history_rows"), "history_rows")
    confidence = _clamp((100 - quality) * 0.15, 0, 15)
    confidence += 1.5 if _boolean(inputs.get("quote_fallback_used"), "quote_fallback_used") else 0.0
    confidence += 1.5 if _boolean(inputs.get("kline_fallback_used"), "kline_fallback_used") else 0.0
    confidence += 1.0 if _boolean(inputs.get("metadata_degraded"), "metadata_degraded") else 0.0
    confidence += 2.0 * _unit(120 - history_rows, 0, 60) if history_rows < 120 else 0.0
    special = (
        (5.0 if _boolean(inputs.get("is_st"), "is_st") else 0.0)
        + (3.0 if _boolean(inputs.get("is_new"), "is_new") else 0.0)
    )
    penalties = {
        "overextension": round(overextension, 8),
        "liquidity": round(liquidity, 8),
        "risk": round(risk, 8),
        "confidence": round(_clamp(confidence, 0, 20), 8),
        "special_status": round(special, 8),
    }
    if variant in SHADOW_SCORE_POINT_IN_TIME_VARIANTS:
        penalties["tradability"] = round(
            _tradability_penalty(
                amount=_finite(inputs.get("amount"), "amount"),
                turnover_rate=_finite(inputs.get("turnover_rate"), "turnover_rate"),
                change_pct=change_pct,
                limit_pct=limit_pct,
                price_limit_quality=str(inputs.get("price_limit_quality") or "degraded"),
            ),
            8,
        )
    return penalties


def _factor_inputs(
    item: ShadowScoreInput,
    board: str,
    history_rows: int,
    return20: float,
    return60: float,
    skip5_return20: float,
    skip5_return55: float,
    return5: float,
    ma20_slope10: float,
    ma5: float,
    ma20: float,
    ma60: float,
    atr_pct: float,
    ma5_atr: float,
    ma20_atr: float,
    range20: float,
    downside_vol: float,
    drawdown: float,
    gap_frequency: float,
    derived_change_pct: float,
    derived_volume_ratio: float,
    limit_context: _PriceLimitContext,
) -> dict[str, float | int | str | bool | None]:
    return {
        "return20_pct": round(return20, 8), "return60_pct": round(return60, 8),
        "skip5_return20_pct": round(skip5_return20, 8),
        "skip5_return55_pct": round(skip5_return55, 8),
        "return5_pct": round(return5, 8),
        "ma20_slope10_pct": round(ma20_slope10, 8), "ma5": round(ma5, 8),
        "ma20": round(ma20, 8), "ma60": round(ma60, 8), "atr20_pct": round(atr_pct, 8),
        "price_vs_ma5_atr": round(ma5_atr, 8), "price_vs_ma20_atr": round(ma20_atr, 8),
        "range_position20": round(range20, 8), "downside_volatility20_pct": round(downside_vol, 8),
        "max_drawdown60_pct": round(drawdown, 8), "gap_frequency60": round(gap_frequency, 8),
        "reported_change_pct": item.change_pct, "derived_change_pct": round(derived_change_pct, 8),
        "price_limit_pct": limit_context.pct, "price_limit_profile_id": limit_context.profile_id,
        "price_limit_quality": limit_context.quality,
        "price_limit_degradation": "|".join(limit_context.degradation_reasons),
        "amount": item.amount, "turnover_rate": item.turnover_rate,
        "reported_volume_ratio": item.volume_ratio,
        "derived_volume_ratio": derived_volume_ratio,
        "volume_ratio_absolute_gap": round(abs(item.volume_ratio - derived_volume_ratio), 8),
        "mode": item.mode,
        "price_window_contract": MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION,
        "market": item.market.upper(),
        "industry": _normalized_industry(item.industry),
        "liquidity_bucket": _shadow_liquidity_bucket(item.amount),
        "notional_to_amount": round(SHADOW_SCORE_NOTIONAL / max(item.amount, 1.0), 10),
        "volume_data_date": item.data_date,
        "volume_price_alignment": (
            "same-completed-session"
            if item.mode in {"official", "preopen"}
            else "intraday-time-aligned-volume-unavailable-neutralized"
        ),
        "data_quality_score": item.data_quality_score,
        "history_rows": history_rows, "is_st": item.is_st, "is_new": item.is_new, "board": board,
        "quote_fallback_used": item.quote_fallback_used,
        "kline_fallback_used": item.kline_fallback_used,
        "metadata_degraded": item.metadata_degraded,
    }


def _normalize_candidate_factor(
    factors: Sequence[_RawFactors],
    variant: ShadowScoreVariant,
) -> tuple[dict[str, float], dict[str, object]]:
    if variant in SHADOW_SCORE_MULTILEVEL_VARIANTS:
        return _normalize_multilevel_residual_rows(
            [
                (
                    item.item.symbol,
                    item.item.market.upper(),
                    item.board,
                    _normalized_industry(item.item.industry),
                    _shadow_liquidity_bucket(item.item.amount),
                    item.skip5_momentum,
                )
                for item in factors
            ]
        )
    rows = [
        (
            item.item.symbol,
            item.board,
            item.skip5_momentum if variant in SHADOW_SCORE_SKIP5_VARIANTS else item.trend_continuation,
        )
        for item in factors
    ]
    if variant in SHADOW_SCORE_RESIDUAL_VARIANTS:
        return _normalize_residual_rows(rows)
    return _normalize_trend_rows(rows)


def _normalize_multilevel_residual_rows(
    rows: Sequence[tuple[str, str, str, str, str, float]],
) -> tuple[dict[str, float], dict[str, object]]:
    if not rows:
        raise ValueError("多层残差评分至少需要一只股票")
    labels = {
        symbol: {"market": market, "board": board, "industry": industry, "liquidity": liquidity}
        for symbol, market, board, industry, liquidity, _value in rows
    }
    values = {symbol: value for symbol, _market, _board_name, _industry, _liquidity, value in rows}
    global_center = _quantile(sorted(values.values()), 0.5)
    residuals = {symbol: value - global_center for symbol, value in values.items()}
    steps: dict[str, object] = {}
    for dimension in ("market", "board", "industry", "liquidity"):
        residuals, steps[dimension] = _residualize_group_dimension(residuals, labels, dimension)
    percentiles = _midrank_percentiles(sorted(residuals.items()))
    normalized = {symbol: round(5 + 90 * value, 8) for symbol, value in percentiles.items()}
    original_pairs = [(symbol, value) for symbol, _market, _board, _industry, _liquidity, value in rows]
    return normalized, {
        "method": "sequential-shrunk-market-board-industry-liquidity-residual-midrank-percentile",
        "output_range": [5.0, 95.0],
        "prior_count": SHADOW_SCORE_NORMALIZATION_PRIOR_COUNT,
        "maximum_group_weight": SHADOW_SCORE_NORMALIZATION_MAX_BOARD_WEIGHT,
        "global_center": round(global_center, 8),
        "steps": steps,
        "input_digest": _pairs_digest(original_pairs),
        "residual_digest": _pairs_digest(sorted(residuals.items())),
    }


def _residualize_group_dimension(
    residuals: Mapping[str, float],
    labels: Mapping[str, Mapping[str, str]],
    dimension: str,
) -> tuple[dict[str, float], dict[str, object]]:
    input_rows = sorted(residuals.items())
    grouped: dict[str, list[tuple[str, float]]] = {}
    for symbol, value in input_rows:
        grouped.setdefault(labels[symbol][dimension], []).append((symbol, value))
    summaries: dict[str, object] = {}
    updated = dict(residuals)
    for group, group_rows in sorted(grouped.items()):
        ordered = sorted(value for _symbol, value in group_rows)
        center = _quantile(ordered, 0.5)
        eligible = dimension != "industry" or _industry_neutralization_eligible(group)
        weight = (
            min(
                SHADOW_SCORE_NORMALIZATION_MAX_BOARD_WEIGHT,
                len(group_rows) / (len(group_rows) + SHADOW_SCORE_NORMALIZATION_PRIOR_COUNT),
            )
            if eligible
            else 0.0
        )
        for symbol, value in group_rows:
            updated[symbol] = value - weight * center
        summaries[group] = {
            "sample_count": len(group_rows), "eligible": eligible, "weight": round(weight, 8),
            "residual_center": round(center, 8), "minimum": ordered[0], "maximum": ordered[-1],
            "input_digest": _pairs_digest(group_rows),
        }
    return updated, {
        "groups": summaries,
        "input_digest": _pairs_digest(input_rows),
        "output_digest": _pairs_digest(sorted(updated.items())),
    }


def _multilevel_normalization_detail(
    normalization: Mapping[str, object],
    inputs: Mapping[str, object],
) -> dict[str, object]:
    context = {
        "market": str(inputs.get("market") or "UNKNOWN"),
        "board": str(inputs.get("board") or "UNKNOWN"),
        "industry": str(inputs.get("industry") or "UNKNOWN"),
        "liquidity": str(inputs.get("liquidity_bucket") or "unknown"),
    }
    steps = _mapping(normalization.get("steps"), "normalization.steps")
    summaries: dict[str, object] = {}
    for dimension, group in context.items():
        step = _mapping(steps.get(dimension), f"normalization.steps.{dimension}")
        groups = _mapping(step.get("groups"), f"normalization.steps.{dimension}.groups")
        summaries[dimension] = groups[group]
    return {
        "context": context,
        "context_summaries": summaries,
        "batch_input_digest": normalization["input_digest"],
        "residual_digest": normalization["residual_digest"],
    }


def _normalize_residual_rows(
    rows: Sequence[tuple[str, str, float]],
) -> tuple[dict[str, float], dict[str, object]]:
    all_values = [value for _symbol, _board_name, value in rows]
    global_center = _quantile(sorted(all_values), 0.5)
    grouped: dict[str, list[tuple[str, float]]] = {}
    for symbol, board_name, value in rows:
        grouped.setdefault(board_name, []).append((symbol, value))
    residual_rows: list[tuple[str, float]] = []
    summaries: dict[str, object] = {}
    for group, values in sorted(grouped.items()):
        ordered = sorted(value for _symbol, value in values)
        board_center = _quantile(ordered, 0.5)
        board_weight = min(
            SHADOW_SCORE_NORMALIZATION_MAX_BOARD_WEIGHT,
            len(values) / (len(values) + SHADOW_SCORE_NORMALIZATION_PRIOR_COUNT),
        )
        center = global_center * (1 - board_weight) + board_center * board_weight
        current_rows = [(symbol, value - center) for symbol, value in values]
        residual_rows.extend(current_rows)
        summaries[group] = {
            "sample_count": len(values),
            "global_sample_count": len(rows),
            "board_weight": round(board_weight, 8),
            "global_weight": round(1 - board_weight, 8),
            "global_center": round(global_center, 8),
            "board_center": round(board_center, 8),
            "blended_center": round(center, 8),
            "minimum": ordered[0],
            "median": board_center,
            "maximum": ordered[-1],
            "board_digest": _pairs_digest(values),
        }
    percentiles = _midrank_percentiles(residual_rows)
    normalized = {symbol: round(5 + 90 * value, 8) for symbol, value in percentiles.items()}
    return normalized, {
        "method": "market-board-centered-residual-midrank-percentile",
        "output_range": [5.0, 95.0],
        "board_prior_count": SHADOW_SCORE_NORMALIZATION_PRIOR_COUNT,
        "maximum_board_weight": SHADOW_SCORE_NORMALIZATION_MAX_BOARD_WEIGHT,
        "groups": summaries,
        "input_digest": _pairs_digest([(symbol, value) for symbol, _board_name, value in rows]),
        "residual_digest": _pairs_digest(residual_rows),
    }


def _normalize_trend_rows(
    rows: Sequence[tuple[str, str, float]],
) -> tuple[dict[str, float], dict[str, object]]:
    all_values = [(symbol, value) for symbol, _board_name, value in rows]
    grouped: dict[str, list[tuple[str, float]]] = {}
    for symbol, board_name, value in rows:
        grouped.setdefault(board_name, []).append((symbol, value))
    global_scores = _midrank_percentiles(all_values)
    normalized: dict[str, float] = {}
    summaries: dict[str, object] = {}
    for group, values in sorted(grouped.items()):
        board_scores = _midrank_percentiles(values)
        board_weight = min(
            SHADOW_SCORE_NORMALIZATION_MAX_BOARD_WEIGHT,
            len(values) / (len(values) + SHADOW_SCORE_NORMALIZATION_PRIOR_COUNT),
        )
        for symbol, _value in values:
            blended = global_scores[symbol] * (1 - board_weight) + board_scores[symbol] * board_weight
            normalized[symbol] = round(5 + 90 * blended, 8)
        ordered_values = sorted(value for _symbol, value in values)
        summaries[group] = {
            "sample_count": len(values),
            "global_sample_count": len(all_values),
            "board_weight": round(board_weight, 8),
            "global_weight": round(1 - board_weight, 8),
            "minimum": ordered_values[0],
            "p25": _quantile(ordered_values, 0.25),
            "median": _quantile(ordered_values, 0.50),
            "p75": _quantile(ordered_values, 0.75),
            "maximum": ordered_values[-1],
            "board_digest": _pairs_digest(values),
            "global_digest": _pairs_digest(all_values),
        }
    return normalized, {
        "method": "hierarchical-global-board-midrank-percentile-shrinkage",
        "output_range": [5.0, 95.0],
        "board_prior_count": SHADOW_SCORE_NORMALIZATION_PRIOR_COUNT,
        "maximum_board_weight": SHADOW_SCORE_NORMALIZATION_MAX_BOARD_WEIGHT,
        "groups": summaries,
        "input_digest": _pairs_digest(all_values),
    }


def _score_one(
    factors: _RawFactors,
    *,
    normalized_trend: float,
    variant: ShadowScoreVariant,
    candidate_id: str,
    spec: dict[str, object],
    spec_hash: str,
    normalization: dict[str, object],
) -> ShadowScoreResult:
    penalties, applied, total_penalty = _applied_score_penalties(factors, variant)
    volume_delta = _applied_volume_delta(factors, variant)
    interaction_delta = _v55_interaction_delta(factors.inputs, normalized_trend, variant)
    raw_score = round(
        _clamp(normalized_trend + volume_delta + interaction_delta - total_penalty, 0, 100),
        SHADOW_SCORE_RAW_DECIMALS,
    )
    details = _score_details(
        factors,
        normalized_trend=normalized_trend,
        candidate_id=candidate_id,
        spec=spec,
        spec_hash=spec_hash,
        normalization=normalization,
        penalties=penalties,
        applied=applied,
        total_penalty=total_penalty,
        raw_score=raw_score,
        variant=variant,
    )
    return ShadowScoreResult(
        symbol=factors.item.symbol,
        rank=0,
        score=max(0, min(100, round(raw_score))),
        raw_score=raw_score,
        variant=variant,
        candidate_id=candidate_id,
        spec_hash=spec_hash,
        board=factors.board,
        details=details,
    )


def _applied_score_penalties(
    factors: _RawFactors,
    variant: ShadowScoreVariant,
) -> tuple[dict[str, float], tuple[bool, bool, bool, bool], float]:
    enabled = _shadow_enabled_penalties(variant)
    penalties = {
        "overextension": factors.overextension_penalty,
        "liquidity": factors.liquidity_penalty,
        "risk": factors.risk_penalty,
        "confidence": factors.confidence_penalty,
        "special_status": factors.special_status_penalty,
    }
    if variant in SHADOW_SCORE_POINT_IN_TIME_VARIANTS:
        penalties["tradability"] = factors.tradability_penalty
    applied = (
        enabled["overextension"], enabled["liquidity"], enabled["risk"], enabled["tradability"],
    )
    total = (
        (penalties["overextension"] if applied[0] else 0)
        + (penalties["liquidity"] if applied[1] else 0)
        + (penalties["risk"] if applied[2] else 0)
        + penalties["confidence"]
        + penalties["special_status"]
        + (penalties.get("tradability", 0.0) if applied[3] else 0.0)
    )
    return penalties, applied, total


def _score_details(
    factors: _RawFactors,
    *,
    normalized_trend: float,
    candidate_id: str,
    spec: dict[str, object],
    spec_hash: str,
    normalization: dict[str, object],
    penalties: dict[str, float],
    applied: tuple[bool, bool, bool, bool],
    total_penalty: float,
    raw_score: float,
    variant: ShadowScoreVariant,
) -> dict[str, object]:
    normalization_detail = _score_normalization_detail(factors, normalization, variant)
    components = _shadow_score_components(
        factors, normalized_trend, penalties, applied, total_penalty, raw_score, variant,
    )
    return {
        "schema_version": _shadow_schema_version(variant),
        "candidate_id": candidate_id,
        "score_spec_hash": spec_hash,
        "score_spec": spec,
        "normalization": normalization_detail,
        "inputs": factors.inputs,
        "components": components,
        "ranking": {"tie_break": [["raw_score", "desc"], ["symbol", "asc"]]},
    }


def _score_normalization_detail(
    factors: _RawFactors,
    normalization: Mapping[str, object],
    variant: ShadowScoreVariant,
) -> dict[str, object]:
    if variant in SHADOW_SCORE_MULTILEVEL_VARIANTS:
        return _multilevel_normalization_detail(normalization, factors.inputs)
    return {
        "group": factors.board,
        "group_summary": _mapping(normalization["groups"], "normalization.groups")[factors.board],
        "batch_input_digest": normalization["input_digest"],
    }


def _shadow_score_components(
    factors: _RawFactors,
    normalized_trend: float,
    penalties: Mapping[str, float],
    applied: tuple[bool, bool, bool, bool],
    total_penalty: float,
    raw_score: float,
    variant: ShadowScoreVariant,
) -> dict[str, object]:
    apply_overextension, apply_liquidity, apply_risk, apply_tradability = applied
    raw_normalization_factor = (
        factors.skip5_momentum if variant in SHADOW_SCORE_SKIP5_VARIANTS else factors.trend_continuation
    )
    applied_volume_delta = _applied_volume_delta(factors, variant)
    challenger_evidence = (
        _v55_challenger_evidence(factors.inputs, normalized_trend)
        if variant in SHADOW_SCORE_V55_VARIANTS
        else None
    )
    components: dict[str, object] = {
        "raw_trend_continuation": factors.trend_continuation,
        "raw_skip5_momentum": factors.skip5_momentum,
        "raw_normalization_factor": raw_normalization_factor,
        "normalized_trend": normalized_trend,
        "normalized_alpha": normalized_trend,
        "volume_confirmation_delta": applied_volume_delta,
        "base_volume_confirmation_delta": factors.volume_confirmation_delta,
        "volume_lifecycle_delta": factors.volume_lifecycle_delta,
        "penalties": penalties,
        "applied_penalties": {
            "overextension": apply_overextension,
            "liquidity": apply_liquidity,
            "risk": apply_risk,
            **({"tradability": apply_tradability} if variant in SHADOW_SCORE_POINT_IN_TIME_VARIANTS else {}),
        },
        "total_penalty": round(total_penalty, 8),
        "raw_score": raw_score,
        "score": round(raw_score),
    }
    if challenger_evidence is not None:
        components["bounded_nonlinear_delta"] = challenger_evidence["bounded_nonlinear_delta"]
        components["challenger_evidence"] = challenger_evidence
    if variant in SHADOW_SCORE_POINT_IN_TIME_VARIANTS:
        components["explicit_constraints"] = {
            "flags": list(factors.constraint_flags),
            "clear": not factors.constraint_flags,
            "ranking_role": "risk-and-tradability-penalties-only-not-alpha",
        }
        components["volume_context"] = {
            "mode": factors.item.mode,
            "basis": "completed-daily-bars-5d-vs-20d",
            "volume_data_date": factors.item.data_date,
            "alignment": factors.inputs["volume_price_alignment"],
            "applied_delta": applied_volume_delta,
        }
    return components


def _v55_interaction_delta(
    inputs: Mapping[str, object],
    normalized_alpha: float,
    variant: ShadowScoreVariant,
) -> float:
    if variant not in SHADOW_SCORE_V55_VARIANTS:
        return 0.0
    evidence = _v55_challenger_evidence(inputs, normalized_alpha)
    return float(cast(float, evidence["bounded_nonlinear_delta"]))


def _v55_challenger_evidence(inputs: Mapping[str, object], normalized_alpha: float) -> dict[str, object]:
    residual_strength = _clamp((normalized_alpha - 50.0) / 45.0, -1.0, 1.0)
    stability_inputs = (
        _clamp(_finite(inputs.get("skip5_return20_pct"), "skip5_return20_pct") / 25.0, -1.0, 1.0),
        _clamp(_finite(inputs.get("skip5_return55_pct"), "skip5_return55_pct") / 50.0, -1.0, 1.0),
        _clamp(_finite(inputs.get("ma20_slope10_pct"), "ma20_slope10_pct") / 12.0, -1.0, 1.0),
    )
    coherence = _clamp(1.0 - pstdev(stability_inputs) / 0.75, 0.0, 1.0)
    directional_strength = abs(fmean(stability_inputs))
    stability_gate = _clamp(0.60 * coherence + 0.40 * directional_strength, 0.0, 1.0)
    turnover_heat = _unit(_finite(inputs.get("turnover_rate"), "turnover_rate"), 15.0, 35.0)
    volume_surge = _unit(_finite(inputs.get("derived_volume_ratio"), "derived_volume_ratio"), 1.5, 4.0)
    range_crowding = _unit(_finite(inputs.get("range_position20"), "range_position20"), 0.85, 1.0)
    crowding_risk = _clamp(0.45 * turnover_heat + 0.35 * volume_surge + 0.20 * range_crowding, 0.0, 1.0)
    notional_to_amount = _finite(inputs.get("notional_to_amount"), "notional_to_amount")
    capacity_risk = _unit(notional_to_amount, 0.002, 0.01)
    confidence_penalty = _replay_penalties(inputs, "v5_5_bounded_nonlinear_stability")["confidence"]
    quality_gate = _clamp(1.0 - confidence_penalty / 20.0, 0.0, 1.0)
    implementability_gate = _clamp(1.0 - 0.55 * crowding_risk - 0.45 * capacity_risk, 0.0, 1.0)
    delta = _v55_bounded_delta(
        residual_strength, stability_gate, quality_gate, implementability_gate,
    )
    return {
        "formula_version": SHADOW_SCORE_V55_ALGORITHM_VERSION,
        "input_digest": _v55_challenger_input_digest(inputs, normalized_alpha),
        "cross_sectional_residual_strength": round(residual_strength, 8),
        "residual_interpretation": (
            "rank-after-market-board-industry-liquidity-residualization-not-industry-breadth"
        ),
        "stability": {
            "short_skip5_strength": round(stability_inputs[0], 8),
            "medium_skip5_strength": round(stability_inputs[1], 8),
            "slope_strength": round(stability_inputs[2], 8),
            "coherence": round(coherence, 8),
            "directional_strength": round(directional_strength, 8),
            "gate": round(stability_gate, 8),
        },
        "crowding": {
            "turnover_heat": round(turnover_heat, 8),
            "completed_volume_surge": round(volume_surge, 8),
            "range_crowding": round(range_crowding, 8),
            "risk": round(crowding_risk, 8),
        },
        "capacity": {
            "notional_to_amount": round(notional_to_amount, 10),
            "risk": round(capacity_risk, 8),
        },
        "quality_gate": round(quality_gate, 8),
        "implementability_gate": round(implementability_gate, 8),
        "positive_residual_gate_applied": residual_strength > 0,
        "negative_residual_policy": (
            "implementability-gate-not-applied-to-negative-delta; conservative-downside-penalty"
        ),
        "bounded_nonlinear_delta": round(delta, 8),
        "bounds": [-6.0, 6.0],
        "training_claim": "none-deterministic-paper-inspired-interaction",
    }


def _v55_bounded_delta(
    residual_strength: float,
    stability_gate: float,
    quality_gate: float,
    implementability_gate: float,
) -> float:
    positive_residual_gate = implementability_gate if residual_strength > 0 else 1.0
    return _clamp(
        6.0 * math.tanh(1.5 * residual_strength) * (0.35 + 0.65 * stability_gate)
        * quality_gate * positive_residual_gate,
        -6.0,
        6.0,
    )


def _v55_challenger_input_digest(inputs: Mapping[str, object], normalized_alpha: float) -> str:
    keys = (
        "skip5_return20_pct", "skip5_return55_pct", "ma20_slope10_pct", "turnover_rate",
        "derived_volume_ratio", "range_position20", "notional_to_amount", "data_quality_score",
        "history_rows", "quote_fallback_used", "kline_fallback_used", "metadata_degraded",
    )
    payload = {
        "normalized_alpha": round(normalized_alpha, 8),
        "inputs": {key: inputs.get(key) for key in keys},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _applied_volume_delta(factors: _RawFactors, variant: ShadowScoreVariant) -> float:
    if factors.item.mode == "intraday":
        return 0.0
    if variant in {"v5_4_skip5_multilevel_residual", "v5_5_bounded_nonlinear_stability"}:
        return 0.0
    if variant == "v5_4_skip5_multilevel_residual_volume_lifecycle":
        return (
            factors.volume_lifecycle_delta
            if factors.item.mode in {"official", "preopen"}
            else 0.0
        )
    if variant == "v5_3_skip5_residual_volume_lifecycle":
        return factors.volume_lifecycle_delta
    return factors.volume_confirmation_delta


def _validated_history(item: ShadowScoreInput) -> tuple[Kline, ...]:
    cutoff, quote_date = _snapshot_dates(item)
    _validate_snapshot_identity(item)
    _validate_snapshot_input(item)
    rows = _history_rows(item.rows, cutoff)
    _validate_history_coverage(item, rows)
    _validate_official_close_consistency(item, rows, quote_date, cutoff)
    return rows


def _history_rows(source: Sequence[Kline], cutoff: date) -> tuple[Kline, ...]:
    by_date: dict[date, Kline] = {}
    for row in source:
        try:
            row_date = date.fromisoformat(row.date)
        except ValueError:
            continue
        if row.date != row_date.isoformat():
            continue
        if row_date <= cutoff and is_trading_day(row_date) and row.adjustment_mode == "qfq" and _valid_bar(row):
            existing = by_date.get(row_date)
            if existing is not None and _bar_signature(existing) != _bar_signature(row):
                raise ValueError(f"影子评分同一交易日 {row.date} 存在冲突日K")
            by_date[row_date] = row
    return tuple(by_date[key] for key in sorted(by_date))


def _validate_history_coverage(item: ShadowScoreInput, rows: Sequence[Kline]) -> None:
    if len(rows) < SHADOW_SCORE_MIN_HISTORY_ROWS:
        raise ValueError(f"{item.symbol} 的影子评分前复权日K不足{SHADOW_SCORE_MIN_HISTORY_ROWS}根")
    if rows[-1].date != item.data_date:
        raise ValueError(f"{item.symbol} 的影子评分日K未截止到 data_date")
    if any(row.volume <= 0 for row in rows[-20:]):
        raise ValueError(f"{item.symbol} 的影子评分近20日存在无成交日，不能进入可比候选榜单")


def _validate_snapshot_input(item: ShadowScoreInput) -> None:
    numeric = (
        item.price,
        item.change_pct,
        item.amount,
        item.volume_ratio,
        float(item.data_quality_score),
    )
    if not all(math.isfinite(float(value)) for value in numeric):
        raise ValueError(f"{item.symbol} 的影子评分输入包含非有限数")
    if (
        item.price <= 0
        or item.amount <= 0
        or item.volume_ratio <= 0
        or item.turnover_rate is None
        or item.turnover_rate < 0
        or not 0 <= item.data_quality_score <= 100
    ):
        raise ValueError(f"{item.symbol} 的影子评分输入不满足准入条件")
    if item.turnover_rate is not None and not math.isfinite(item.turnover_rate):
        raise ValueError(f"{item.symbol} 的换手率不是有限数")


def _valid_bar(row: Kline) -> bool:
    values = (row.open, row.close, row.high, row.low, row.volume)
    return (
        all(math.isfinite(float(value)) for value in values)
        and row.open > 0
        and row.close > 0
        and row.high >= max(row.open, row.close, row.low)
        and row.low <= min(row.open, row.close, row.high)
        and row.volume >= 0
    )


def _snapshot_dates(item: ShadowScoreInput) -> tuple[date, date]:
    parsed: dict[str, date] = {}
    for label, value in (("data_date", item.data_date), ("quote_date", item.quote_date)):
        try:
            current = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{item.symbol} 的 {label} 无效") from exc
        if current.isoformat() != value:
            raise ValueError(f"{item.symbol} 的 {label} 不是规范日期")
        parsed[label] = current
    if parsed["quote_date"] < parsed["data_date"]:
        raise ValueError(f"{item.symbol} 的 quote_date 早于 data_date")
    return parsed["data_date"], parsed["quote_date"]


def _validate_v54_temporal_context(items: Sequence[ShadowScoreInput]) -> None:
    for item in items:
        if item.mode not in {"official", "intraday", "preopen"}:
            raise ValueError(f"{item.symbol} 的扫描模式无效")
        data_date, quote_date = _snapshot_dates(item)
        if item.mode in {"official", "preopen"} and quote_date != data_date:
            label = "盘前" if item.mode == "preopen" else "盘后"
            raise ValueError(f"{item.symbol} 的{label}候选要求 quote_date 与 data_date 一致")
        if item.mode == "intraday" and quote_date <= data_date:
            raise ValueError(f"{item.symbol} 的盘中候选要求 quote_date 晚于完整日K截止日")


def _validate_snapshot_identity(item: ShadowScoreInput) -> None:
    try:
        canonical = standard_symbol(item.symbol)
    except ValueError as exc:
        raise ValueError(f"{item.symbol} 不是有效A股代码") from exc
    if canonical != item.symbol or canonical.rsplit(".", 1)[-1] != item.market.upper():
        raise ValueError(f"{item.symbol} 的股票代码与市场字段不一致")


def _validate_official_close_consistency(
    item: ShadowScoreInput,
    rows: Sequence[Kline],
    quote_date: date,
    data_date: date,
) -> None:
    if quote_date != data_date:
        return
    latest_close = float(rows[-1].close)
    gap = abs(item.price - latest_close)
    relative_limit = max(item.price, latest_close) * SHADOW_SCORE_MAX_OFFICIAL_CLOSE_GAP_PCT / 100
    if gap > max(SHADOW_SCORE_MAX_OFFICIAL_CLOSE_GAP_ABSOLUTE, relative_limit):
        raise ValueError(f"{item.symbol} 的冻结报价与同日历史K线收盘价不一致")


def _snapshot_change_pct(item: ShadowScoreInput, rows: Sequence[Kline], current: float) -> float:
    _snapshot_dates(item)
    return snapshot_return_pct(
        current,
        [float(row.close) for row in rows],
        horizon=1,
        mode=item.mode,
    )


def _bar_signature(row: Kline) -> tuple[float, float, float, float, float, str]:
    return (row.open, row.close, row.high, row.low, row.volume, row.adjustment_mode)


def _liquidity_penalty(amount: float, turnover_rate: float | None) -> float:
    amount_penalty = 10 * _unit(math.log10(50_000_000) - math.log10(max(amount, 1.0)), 0, 1)
    capacity_ratio = SHADOW_SCORE_NOTIONAL / amount
    capacity_penalty = 3 * _unit(capacity_ratio, 0.002, 0.02)
    turnover = float(turnover_rate or 0.0)
    low_turnover = 2 * _unit(0.5 - turnover, 0, 0.5)
    high_turnover = 4 * _unit(turnover, 12, 30)
    return _clamp(amount_penalty + capacity_penalty + low_turnover + high_turnover, 0, 15)


def _midrank_percentiles(values: Sequence[tuple[str, float]]) -> dict[str, float]:
    ordered = sorted(values, key=lambda item: (item[1], item[0]))
    if len(ordered) == 1:
        return {ordered[0][0]: 0.5}
    result: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        midrank = (index + end - 1) / 2
        percentile = midrank / (len(ordered) - 1)
        for position in range(index, end):
            result[ordered[position][0]] = percentile
        index = end
    return result


def _verify_shadow_result_context(item: ShadowScoreResult, batch: ShadowScoreBatch) -> None:
    details = item.details
    components = _mapping(details.get("components"), "components")
    inputs = _mapping(details.get("inputs"), "inputs")
    if (
        item.candidate_id != batch.candidate_id
        or item.variant != batch.variant
        or item.spec_hash != batch.spec_hash
        or details.get("candidate_id") != batch.candidate_id
        or details.get("score_spec_hash") != batch.spec_hash
        or details.get("score_spec") != batch.spec
    ):
        raise ShadowScoreReplayError("影子评分结果与批次候选上下文不一致")
    if inputs.get("board") != item.board:
        raise ShadowScoreReplayError("影子评分结果板块上下文不一致")
    persisted_raw = _finite(components.get("raw_score"), "raw_score")
    if not math.isclose(item.raw_score, persisted_raw, rel_tol=0, abs_tol=10 ** (-SHADOW_SCORE_RAW_DECIMALS)):
        raise ShadowScoreReplayError("影子评分结果 raw_score 与明细不一致")
    if item.score != max(0, min(100, round(item.raw_score))):
        raise ShadowScoreReplayError("影子评分结果整数分与 raw_score 不一致")


def _shadow_variant(value: object) -> ShadowScoreVariant:
    if value in SHADOW_SCORE_VARIANTS:
        return cast(ShadowScoreVariant, value)
    raise ShadowScoreReplayError("影子评分规范版本不受支持")


def _require_component_range(value: float, minimum: float, maximum: float, label: str) -> None:
    if not minimum <= value <= maximum:
        raise ShadowScoreReplayError(f"{label} 超出评分规范边界")


def _component_isclose(left: float, right: float) -> bool:
    return math.isclose(
        left,
        right,
        rel_tol=0,
        abs_tol=SHADOW_SCORE_COMPONENT_REPLAY_TOLERANCE,
    )


def _pairs_digest(values: Sequence[tuple[str, float]]) -> str:
    payload = [[symbol, round(value, 8)] for symbol, value in sorted(values)]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("quantile requires values")
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _atr(rows: Sequence[Kline]) -> float:
    ranges = [
        max(
            float(row.high) - float(row.low),
            abs(float(row.high) - float(previous.close)),
            abs(float(row.low) - float(previous.close)),
        )
        for previous, row in zip(rows[:-1], rows[1:], strict=True)
    ]
    return fmean(ranges) if ranges else 0.0


def _max_drawdown(values: Sequence[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1 if peak > 0 else 0.0)
    return worst


def _range_position(value: float, low: float, high: float) -> float:
    return _clamp((value - low) / (high - low), 0, 1) if high > low else 0.5


def _pct_change(value: float, reference: float) -> float:
    return _return(value, reference) * 100


def _return(value: float, reference: float) -> float:
    return value / reference - 1 if reference > 0 else 0.0


def _unit(value: float, lower: float, upper: float) -> float:
    if upper <= lower:
        return 0.0
    return _clamp((value - lower) / (upper - lower), 0, 1)


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _board(symbol: str, market: str) -> str:
    code = symbol.split(".", 1)[0]
    normalized_market = market.upper()
    if normalized_market == "BJ":
        return "BSE"
    if normalized_market == "SH" and code.startswith(("688", "689")):
        return "STAR"
    if normalized_market == "SZ" and code.startswith(("300", "301")):
        return "CHINEXT"
    return f"{normalized_market}_MAIN"


_BROAD_OR_UNRELIABLE_INDUSTRIES = frozenset(
    {
        "UNKNOWN",
        "制造业",
        "信息技术",
        "金融业",
        "建筑业",
        "采矿业",
        "房地产业",
        "综合",
    }
)


def _normalized_industry(value: object) -> str:
    normalized = "".join(str(value or "").split()).strip("-/、，")
    aliases = {
        "信息传输、软件和信息技术服务业": "信息技术",
        "软件和信息技术服务业": "信息技术",
        "信息传输软件和信息技术服务业": "信息技术",
    }
    return aliases.get(normalized, normalized) if normalized else "UNKNOWN"


def _industry_neutralization_eligible(industry: str) -> bool:
    return industry not in _BROAD_OR_UNRELIABLE_INDUSTRIES


def _shadow_liquidity_bucket(amount: float) -> str:
    if amount >= 1_000_000_000:
        return "high"
    if amount >= 100_000_000:
        return "medium"
    return "low"


def _price_limit_context(item: ShadowScoreInput) -> _PriceLimitContext:
    metadata = PaperInstrumentMetadata(
        symbol=item.symbol,
        market=item.market,
        list_date=item.list_date,
        is_st=item.is_st,
        status_effective_date=item.quote_date,
        source="shadow-score-snapshot-profile",
    )
    try:
        profile = resolve_trade_rule_profile(item.symbol, date.fromisoformat(item.quote_date), metadata)
    except (KeyError, ValueError):
        return _PriceLimitContext(None, "unavailable", "degraded", ("rule_profile_unavailable",))
    return _PriceLimitContext(
        profile.price_limit_pct,
        profile.profile_id,
        profile.quality,
        tuple(profile.degradation_reasons),
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ShadowScoreReplayError(f"{label} 必须是对象")
    return value


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ShadowScoreReplayError(f"{label} 必须是有限数")
    return float(value)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ShadowScoreReplayError(f"{label} 必须是布尔值")
    return value


__all__ = [
    "SHADOW_SCORE_ALGORITHM_VERSION",
    "SHADOW_SCORE_CANDIDATE_VERSION",
    "SHADOW_SCORE_MIN_HISTORY_ROWS",
    "SHADOW_SCORE_SCHEMA_VERSION",
    "SHADOW_SCORE_VARIANTS",
    "SHADOW_SCORE_V55_ALGORITHM_VERSION",
    "SHADOW_SCORE_V55_CANDIDATE_VERSION",
    "SHADOW_SCORE_V55_SCHEMA_VERSION",
    "ShadowScoreBatch",
    "ShadowScoreInput",
    "ShadowScoreReplayError",
    "ShadowScoreResult",
    "market_scan_shadow_score_spec",
    "replay_shadow_score_details",
    "score_shadow_market",
    "stable_shadow_spec_hash",
    "verify_shadow_score_batch",
]
