from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import AnalysisResult, FeatureSnapshot


@dataclass(frozen=True)
class FactorWeightContext:
    amount: float
    market_cap: float
    turnover: float | None
    volume_ratio: float
    data_quality_score: int


@dataclass(frozen=True)
class FactorWeightProfile:
    label: str
    adjustments: dict[str, float]
    note: str
    matches: Callable[[FactorWeightContext], bool]


DEFAULT_FACTOR_PROFILE = "常规个股"
DEFAULT_FACTOR_WEIGHT_NOTE = "使用默认单股分析权重。"
LOW_QUALITY_WEIGHT_NOTE = "数据质量不足时提高风控权重，降低资金估算权重。"
LOW_QUALITY_THRESHOLD = 70
LOW_QUALITY_ADJUSTMENTS = {"risk_pressure": 1.18, "fund_flow_proxy": 0.88}
WEIGHT_MULTIPLIER_MIN = 0.5
WEIGHT_MULTIPLIER_MAX = 1.8

FACTOR_WEIGHT_PROFILES = (
    FactorWeightProfile(
        label="大市值稳健股",
        adjustments={"valuation_anchor": 1.25, "risk_pressure": 1.12, "trend_momentum": 1.08, "leadership_strength": 0.9},
        note="大市值稳健股提高估值锚、风控和趋势修复权重，降低短线情绪权重。",
        matches=lambda context: _is_large_stable_stock(context),
    ),
    FactorWeightProfile(
        label="高活跃波动股",
        adjustments={"volume_confirmation": 1.25, "fund_flow_proxy": 1.15, "risk_pressure": 1.18, "valuation_anchor": 0.82},
        note="高活跃波动股提高量价、资金和风险权重，降低静态估值权重。",
        matches=lambda context: _is_high_activity_stock(context),
    ),
    FactorWeightProfile(
        label="低流动性个股",
        adjustments={"risk_pressure": 1.28, "volume_confirmation": 1.15, "fund_flow_proxy": 0.86, "leadership_strength": 0.88},
        note="低流动性个股提高风险和量价确认权重，降低资金估算与强弱标签权重。",
        matches=lambda context: _is_low_liquidity_stock(context),
    ),
)


def _factor_weight_policy(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
) -> tuple[str, dict[str, float], list[str]]:
    context = _factor_weight_context(analysis, feature)
    profile, adjustments, notes = _matched_factor_weight_policy(context)
    _apply_low_quality_adjustments(context, adjustments, notes)
    return profile, adjustments, notes or [DEFAULT_FACTOR_WEIGHT_NOTE]


def _adjusted_factor_weight(factor_id: str, base_weight: float, adjustments: dict[str, float]) -> float:
    multiplier = adjustments.get(factor_id, 1.0)
    return round(max(WEIGHT_MULTIPLIER_MIN, min(WEIGHT_MULTIPLIER_MAX, base_weight * multiplier)), 2)


def _factor_weight_context(analysis: AnalysisResult, feature: FeatureSnapshot) -> FactorWeightContext:
    return FactorWeightContext(
        amount=feature.amount or 0,
        market_cap=analysis.quote.market_cap or 0,
        turnover=feature.turnover_rate,
        volume_ratio=feature.volume_ratio,
        data_quality_score=feature.data_quality_score,
    )


def _matched_factor_weight_policy(context: FactorWeightContext) -> tuple[str, dict[str, float], list[str]]:
    profile = next((profile for profile in FACTOR_WEIGHT_PROFILES if profile.matches(context)), None)
    if not profile:
        return DEFAULT_FACTOR_PROFILE, {}, []
    return profile.label, dict(profile.adjustments), [profile.note]


def _apply_low_quality_adjustments(
    context: FactorWeightContext,
    adjustments: dict[str, float],
    notes: list[str],
) -> None:
    if context.data_quality_score >= LOW_QUALITY_THRESHOLD:
        return
    for factor_id, multiplier in LOW_QUALITY_ADJUSTMENTS.items():
        adjustments[factor_id] = adjustments.get(factor_id, 1.0) * multiplier
    notes.append(LOW_QUALITY_WEIGHT_NOTE)


def _is_large_stable_stock(context: FactorWeightContext) -> bool:
    return context.market_cap >= 500_000_000_000 or (
        context.amount >= 3_000_000_000
        and context.turnover is not None
        and context.turnover < 2
    )


def _is_high_activity_stock(context: FactorWeightContext) -> bool:
    return (
        context.turnover is not None and context.turnover >= 8
    ) or context.volume_ratio >= 1.6


def _is_low_liquidity_stock(context: FactorWeightContext) -> bool:
    return bool(context.amount) and context.amount < 300_000_000


__all__ = ["_adjusted_factor_weight", "_factor_weight_policy"]
