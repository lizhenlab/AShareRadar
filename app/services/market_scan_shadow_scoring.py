"""Read-only, cross-sectional shadow scoring for full-market research.

This module is deliberately disconnected from the production scan write path.  It
turns an immutable published snapshot plus daily bars that were available at the
snapshot cutoff into a replayable candidate ranking.  The candidate is evidence
for later review; it is never a production rule promotion mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
import hashlib
import json
import math
from statistics import fmean, pstdev
from typing import Literal, Mapping, Sequence

from app.models.market import Kline
from app.models.paper_trading import PaperInstrumentMetadata
from app.services.paper_trading_rules import resolve_trade_rule_profile


SHADOW_SCORE_SCHEMA_VERSION = 1
SHADOW_SCORE_CANDIDATE_VERSION = "full-market-shadow-score-v5"
SHADOW_SCORE_ALGORITHM_VERSION = "bounded-price-volume-risk-v1"
SHADOW_SCORE_RAW_DECIMALS = 6
SHADOW_SCORE_NOTIONAL = 100_000.0
ShadowScoreVariant = Literal[
    "v5_full",
    "v5_without_overextension",
    "v5_without_risk",
    "v5_without_liquidity",
]
SHADOW_SCORE_VARIANTS: tuple[ShadowScoreVariant, ...] = (
    "v5_full",
    "v5_without_overextension",
    "v5_without_risk",
    "v5_without_liquidity",
)


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
    volume_confirmation_delta: float
    overextension_penalty: float
    liquidity_penalty: float
    risk_penalty: float
    confidence_penalty: float
    special_status_penalty: float
    inputs: dict[str, float | int | str | bool | None]


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

    raw = tuple(_raw_factors(item) for item in items)
    normalized, normalization = _normalize_trend_by_board(raw)
    spec = market_scan_shadow_score_spec(variant=variant)
    spec_hash = stable_shadow_spec_hash(spec)
    candidate_id = f"{SHADOW_SCORE_CANDIDATE_VERSION}:{variant}:{spec_hash}"
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
    spec = _mapping(details.get("score_spec"), "score_spec")
    expected_hash = stable_shadow_spec_hash(spec)
    if details.get("score_spec_hash") != expected_hash:
        raise ShadowScoreReplayError("影子评分规范哈希不一致")
    components = _mapping(details.get("components"), "components")
    normalized_trend = _finite(components.get("normalized_trend"), "normalized_trend")
    volume_delta = _finite(components.get("volume_confirmation_delta"), "volume_confirmation_delta")
    penalties = _mapping(components.get("penalties"), "penalties")
    applied = _mapping(components.get("applied_penalties"), "applied_penalties")
    expected_applied = {
        "overextension": bool(spec["components"]["overextension_penalty"]["enabled"]),  # type: ignore[index]
        "liquidity": bool(spec["components"]["liquidity_penalty"]["enabled"]),  # type: ignore[index]
        "risk": bool(spec["components"]["risk_penalty"]["enabled"]),  # type: ignore[index]
    }
    if dict(applied) != expected_applied:
        raise ShadowScoreReplayError("影子评分消融开关与规范不一致")
    total_penalty = (
        (_finite(penalties.get("overextension"), "overextension") if expected_applied["overextension"] else 0.0)
        + (_finite(penalties.get("liquidity"), "liquidity") if expected_applied["liquidity"] else 0.0)
        + (_finite(penalties.get("risk"), "risk") if expected_applied["risk"] else 0.0)
        + _finite(penalties.get("confidence"), "confidence")
        + _finite(penalties.get("special_status"), "special_status")
    )
    raw = round(_clamp(normalized_trend + volume_delta - total_penalty, 0.0, 100.0), SHADOW_SCORE_RAW_DECIMALS)
    persisted = _finite(components.get("raw_score"), "raw_score")
    if not math.isclose(raw, persisted, rel_tol=0, abs_tol=10 ** (-SHADOW_SCORE_RAW_DECIMALS)):
        raise ShadowScoreReplayError(f"影子评分重放不一致：{raw} != {persisted}")
    return raw


def verify_shadow_score_batch(batch: ShadowScoreBatch) -> None:
    if batch.spec_hash != stable_shadow_spec_hash(batch.spec):
        raise ShadowScoreReplayError("影子评分批次规范哈希不一致")
    expected = sorted(batch.results, key=lambda item: (-replay_shadow_score_details(item.details), item.symbol))
    if [item.symbol for item in expected] != [item.symbol for item in batch.results]:
        raise ShadowScoreReplayError("影子评分批次排名无法重放")
    if [item.rank for item in batch.results] != list(range(1, len(batch.results) + 1)):
        raise ShadowScoreReplayError("影子评分批次名次不连续")
    normalization = _mapping(batch.normalization, "normalization")
    if normalization.get("input_digest") != _normalization_digest_from_details(batch.results):
        raise ShadowScoreReplayError("影子评分横截面归一化摘要不一致")


def market_scan_shadow_score_spec(
    *,
    variant: ShadowScoreVariant = "v5_full",
) -> dict[str, object]:
    if variant not in SHADOW_SCORE_VARIANTS:
        raise ValueError(f"未知影子评分消融版本：{variant}")
    enabled = {
        "overextension": variant != "v5_without_overextension",
        "risk": variant != "v5_without_risk",
        "liquidity": variant != "v5_without_liquidity",
    }
    return {
        "schema_version": SHADOW_SCORE_SCHEMA_VERSION,
        "candidate_version": SHADOW_SCORE_CANDIDATE_VERSION,
        "algorithm": SHADOW_SCORE_ALGORITHM_VERSION,
        "variant": variant,
        "purpose": "read-only-shadow-research-not-production",
        "inputs": {
            "price_history": "qfq_daily_rows_not_after_data_date",
            "snapshot_quote": "persisted_market_scan_result",
            "minimum_history_rows": 60,
            "notional": SHADOW_SCORE_NOTIONAL,
        },
        "normalization": {
            "factor": "trend_continuation",
            "method": "deterministic-midrank-percentile",
            "group": "exchange-board",
            "output_range": [5.0, 95.0],
            "minimum_group_size": 30,
            "fallback_group": "ALL",
        },
        "components": _shadow_component_spec(enabled),
        "final_score": {
            "formula": "normalized_trend + volume_confirmation_delta - enabled_penalties",
            "clamp": [0, 100],
            "raw_decimals": SHADOW_SCORE_RAW_DECIMALS,
            "tie_break": [["raw_score", "desc"], ["symbol", "asc"]],
        },
        "promotion": "forbidden-without-independent-session-evidence",
    }


def _shadow_component_spec(enabled: Mapping[str, bool]) -> dict[str, object]:
    return {
        "trend_continuation": {
            "formula": "45% return20 + 35% return60 + 20% ma20_slope10",
            "raw_bounds": {"return20": [-15, 25], "return60": [-25, 50], "ma20_slope10": [-8, 12]},
            "role": "alpha",
        },
        "volume_confirmation_delta": {"bounds": [-6, 6], "role": "signed-alpha-confirmation"},
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


def stable_shadow_spec_hash(spec: Mapping[str, object]) -> str:
    payload = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _raw_factors(item: ShadowScoreInput) -> _RawFactors:
    rows = _validated_history(item)
    closes = [float(row.close) for row in rows]
    current = float(item.price)
    ma5 = fmean(closes[-5:])
    ma20 = fmean(closes[-20:])
    ma60 = fmean(closes[-60:])
    return20 = _pct_change(current, closes[-21])
    return60 = _pct_change(current, closes[-60])
    ma20_slope10 = _pct_change(ma20, fmean(closes[-30:-10]))
    trend = 100 * (
        0.45 * _unit(return20, -15, 25)
        + 0.35 * _unit(return60, -25, 50)
        + 0.20 * _unit(ma20_slope10, -8, 12)
    )
    atr20 = _atr(rows[-21:])
    atr_pct = atr20 / current * 100 if current > 0 else 100.0
    range20 = _range_position(current, min(row.low for row in rows[-20:]), max(row.high for row in rows[-20:]))
    board = _board(item.symbol, item.market)
    limit_pct = _price_limit_pct(item, board)
    ma5_atr = max(0.0, current - ma5) / atr20 if atr20 > 0 else 0.0
    ma20_atr = max(0.0, current - ma20) / atr20 if atr20 > 0 else 0.0
    overextension = _overextension_penalty(item.change_pct, limit_pct, ma5_atr, ma20_atr, range20)
    direction = _clamp(item.change_pct / 3.0, -1.0, 1.0)
    safe_volume_ratio = max(0.05, float(item.volume_ratio))
    volume_delta = _clamp(6 * math.tanh(math.log(safe_volume_ratio)) * direction, -6, 6)
    liquidity_penalty = _liquidity_penalty(item.amount, item.turnover_rate)
    risk, downside_vol, drawdown, gap_frequency = _risk_penalty(rows, closes, atr_pct)
    confidence = _confidence_penalty(item, len(rows))
    special = (5.0 if item.is_st else 0.0) + (3.0 if item.is_new else 0.0)
    return _RawFactors(
        item=item,
        board=board,
        trend_continuation=round(trend, 8),
        volume_confirmation_delta=round(volume_delta, 8),
        overextension_penalty=round(overextension, 8),
        liquidity_penalty=round(liquidity_penalty, 8),
        risk_penalty=round(risk, 8),
        confidence_penalty=round(confidence, 8),
        special_status_penalty=round(special, 8),
        inputs=_factor_inputs(
            item, board, len(rows), return20, return60, ma20_slope10, ma5, ma20, ma60,
            atr_pct, ma5_atr, ma20_atr, range20, downside_vol, drawdown, gap_frequency, limit_pct,
        ),
    )


def _overextension_penalty(
    change_pct: float,
    limit_pct: float | None,
    ma5_atr: float,
    ma20_atr: float,
    range20: float,
) -> float:
    effective_limit = float(limit_pct or 20.0)
    limit_progress = _unit(max(0.0, change_pct), effective_limit * 0.55, effective_limit * 0.98)
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
    gap_rows = rows[-61:]
    gaps = [abs(_return(float(row.open), float(previous.close))) for previous, row in zip(gap_rows[:-1], gap_rows[1:], strict=True)]
    gap_frequency = sum(value >= 0.03 for value in gaps) / len(gaps) if gaps else 0.0
    risk = _clamp(
        5 * _unit(atr_pct, 2, 10)
        + 4 * _unit(downside_vol, 1, 5)
        + 4 * _unit(drawdown, 8, 35)
        + 2 * _unit(gap_frequency, 0.02, 0.20),
        0,
        15,
    )
    return risk, downside_vol, drawdown, gap_frequency


def _confidence_penalty(item: ShadowScoreInput, history_rows: int) -> float:
    penalty = _clamp((100 - item.data_quality_score) * 0.15, 0, 15)
    penalty += 1.5 if item.quote_fallback_used else 0.0
    penalty += 1.5 if item.kline_fallback_used else 0.0
    penalty += 1.0 if item.metadata_degraded else 0.0
    penalty += 2.0 * _unit(120 - history_rows, 0, 60) if history_rows < 120 else 0.0
    return _clamp(penalty, 0, 20)


def _factor_inputs(
    item: ShadowScoreInput,
    board: str,
    history_rows: int,
    return20: float,
    return60: float,
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
    limit_pct: float | None,
) -> dict[str, float | int | str | bool | None]:
    return {
        "return20_pct": round(return20, 8), "return60_pct": round(return60, 8),
        "ma20_slope10_pct": round(ma20_slope10, 8), "ma5": round(ma5, 8),
        "ma20": round(ma20, 8), "ma60": round(ma60, 8), "atr20_pct": round(atr_pct, 8),
        "price_vs_ma5_atr": round(ma5_atr, 8), "price_vs_ma20_atr": round(ma20_atr, 8),
        "range_position20": round(range20, 8), "downside_volatility20_pct": round(downside_vol, 8),
        "max_drawdown60_pct": round(drawdown, 8), "gap_frequency60": round(gap_frequency, 8),
        "price_limit_pct": limit_pct, "amount": item.amount, "turnover_rate": item.turnover_rate,
        "volume_ratio": item.volume_ratio, "data_quality_score": item.data_quality_score,
        "history_rows": history_rows, "is_st": item.is_st, "is_new": item.is_new, "board": board,
    }


def _normalize_trend_by_board(
    factors: Sequence[_RawFactors],
) -> tuple[dict[str, float], dict[str, object]]:
    all_values = [(item.item.symbol, item.trend_continuation) for item in factors]
    grouped: dict[str, list[tuple[str, float]]] = {}
    for item in factors:
        grouped.setdefault(item.board, []).append((item.item.symbol, item.trend_continuation))
    normalized: dict[str, float] = {}
    summaries: dict[str, object] = {}
    for group, values in sorted(grouped.items()):
        reference = values if len(values) >= 30 else all_values
        reference_group = group if len(values) >= 30 else "ALL"
        scores = _midrank_percentiles(reference)
        for symbol, _value in values:
            normalized[symbol] = round(5 + 90 * scores[symbol], 8)
        ordered_values = sorted(value for _symbol, value in reference)
        summaries[group] = {
            "reference_group": reference_group,
            "sample_count": len(reference),
            "minimum": ordered_values[0],
            "p25": _quantile(ordered_values, 0.25),
            "median": _quantile(ordered_values, 0.50),
            "p75": _quantile(ordered_values, 0.75),
            "maximum": ordered_values[-1],
            "reference_digest": _pairs_digest(reference),
        }
    return normalized, {
        "method": "deterministic-midrank-percentile",
        "output_range": [5.0, 95.0],
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
    apply_overextension = variant != "v5_without_overextension"
    apply_risk = variant != "v5_without_risk"
    apply_liquidity = variant != "v5_without_liquidity"
    penalties = {
        "overextension": factors.overextension_penalty,
        "liquidity": factors.liquidity_penalty,
        "risk": factors.risk_penalty,
        "confidence": factors.confidence_penalty,
        "special_status": factors.special_status_penalty,
    }
    total_penalty = (
        (penalties["overextension"] if apply_overextension else 0)
        + (penalties["liquidity"] if apply_liquidity else 0)
        + (penalties["risk"] if apply_risk else 0)
        + penalties["confidence"]
        + penalties["special_status"]
    )
    raw_score = round(
        _clamp(normalized_trend + factors.volume_confirmation_delta - total_penalty, 0, 100),
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
        applied=(apply_overextension, apply_liquidity, apply_risk),
        total_penalty=total_penalty,
        raw_score=raw_score,
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


def _score_details(
    factors: _RawFactors,
    *,
    normalized_trend: float,
    candidate_id: str,
    spec: dict[str, object],
    spec_hash: str,
    normalization: dict[str, object],
    penalties: dict[str, float],
    applied: tuple[bool, bool, bool],
    total_penalty: float,
    raw_score: float,
) -> dict[str, object]:
    apply_overextension, apply_liquidity, apply_risk = applied
    return {
        "schema_version": SHADOW_SCORE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "score_spec_hash": spec_hash,
        "score_spec": spec,
        "normalization": {
            "group": factors.board,
            "group_summary": _mapping(normalization["groups"], "normalization.groups")[factors.board],
            "batch_input_digest": normalization["input_digest"],
        },
        "inputs": factors.inputs,
        "components": {
            "raw_trend_continuation": factors.trend_continuation,
            "normalized_trend": normalized_trend,
            "volume_confirmation_delta": factors.volume_confirmation_delta,
            "penalties": penalties,
            "applied_penalties": {
                "overextension": apply_overextension,
                "liquidity": apply_liquidity,
                "risk": apply_risk,
            },
            "total_penalty": round(total_penalty, 8),
            "raw_score": raw_score,
            "score": round(raw_score),
        },
        "ranking": {"tie_break": [["raw_score", "desc"], ["symbol", "asc"]]},
    }


def _validated_history(item: ShadowScoreInput) -> tuple[Kline, ...]:
    try:
        cutoff = date.fromisoformat(item.data_date)
    except ValueError as exc:
        raise ValueError(f"{item.symbol} 的 data_date 无效") from exc
    rows = _history_rows(item.rows, cutoff)
    _validate_history_coverage(item, rows)
    _validate_snapshot_input(item)
    return rows


def _history_rows(source: Sequence[Kline], cutoff: date) -> tuple[Kline, ...]:
    by_date: dict[str, Kline] = {}
    for row in source:
        try:
            row_date = date.fromisoformat(row.date)
        except ValueError:
            continue
        if row_date <= cutoff and row.adjustment_mode == "qfq" and _valid_bar(row):
            by_date[row.date] = row
    return tuple(by_date[key] for key in sorted(by_date))


def _validate_history_coverage(item: ShadowScoreInput, rows: Sequence[Kline]) -> None:
    if len(rows) < 60:
        raise ValueError(f"{item.symbol} 的影子评分前复权日K不足60根")
    if rows[-1].date != item.data_date:
        raise ValueError(f"{item.symbol} 的影子评分日K未截止到 data_date")
    if rows[-1].volume <= 0:
        raise ValueError(f"{item.symbol} 的影子评分日无成交，不能进入候选榜单")


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
    if item.price <= 0 or item.amount <= 0 or not 0 <= item.data_quality_score <= 100:
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


def _normalization_digest_from_details(results: Sequence[ShadowScoreResult]) -> str:
    pairs = [
        (
            item.symbol,
            _finite(_mapping(item.details["components"], "components")["raw_trend_continuation"], "raw_trend"),
        )
        for item in results
    ]
    return _pairs_digest(pairs)


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


def _price_limit_pct(item: ShadowScoreInput, board: str) -> float | None:
    if item.is_st or item.is_new:
        return None
    return _standard_price_limit(board, item.quote_date)


@lru_cache(maxsize=128)
def _standard_price_limit(board: str, quote_date: str) -> float | None:
    canonical = {
        "SH_MAIN": ("600001.SH", "SH"),
        "STAR": ("688001.SH", "SH"),
        "SZ_MAIN": ("000001.SZ", "SZ"),
        "CHINEXT": ("300001.SZ", "SZ"),
        "BSE": ("920001.BJ", "BJ"),
    }.get(board)
    if canonical is None:
        return None
    symbol, market = canonical
    metadata = PaperInstrumentMetadata(
        symbol=symbol,
        market=market,
        list_date="2020-01-02",
        is_st=False,
        status_effective_date=quote_date,
        source="shadow-score-standard-profile",
    )
    try:
        return resolve_trade_rule_profile(symbol, date.fromisoformat(quote_date), metadata).price_limit_pct
    except (KeyError, ValueError):
        return None


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ShadowScoreReplayError(f"{label} 必须是对象")
    return value


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ShadowScoreReplayError(f"{label} 必须是有限数")
    return float(value)


__all__ = [
    "SHADOW_SCORE_ALGORITHM_VERSION",
    "SHADOW_SCORE_CANDIDATE_VERSION",
    "SHADOW_SCORE_SCHEMA_VERSION",
    "SHADOW_SCORE_VARIANTS",
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
