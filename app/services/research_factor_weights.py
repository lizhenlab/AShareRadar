from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.analysis import (
    AnalysisResult,
    FeatureSnapshot,
)


@dataclass(frozen=True)
class FactorWeightContext:
    amount: float
    market_cap: float
    turnover: float | None
    volume_ratio: float


@dataclass(frozen=True)
class FactorWeightProfile:
    label: str
    matches: Callable[[FactorWeightContext], bool]


DEFAULT_FACTOR_PROFILE = "常规个股"
DEFAULT_FACTOR_WEIGHT_NOTE = "画像只作说明，使用固定方向预算；风险单独约束，数据质量单独披露。"
WEIGHT_MULTIPLIER_MIN = 0.5
WEIGHT_MULTIPLIER_MAX = 1.8

FACTOR_WEIGHT_PROFILES = (
    FactorWeightProfile(
        label="大市值稳健股",
        matches=lambda context: _is_large_stable_stock(context),
    ),
    FactorWeightProfile(
        label="高活跃波动股",
        matches=lambda context: _is_high_activity_stock(context),
    ),
    FactorWeightProfile(
        label="低流动性个股",
        matches=lambda context: _is_low_liquidity_stock(context),
    ),
)


def _factor_weight_policy(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
) -> tuple[str, dict[str, float], list[str]]:
    context = _factor_weight_context(analysis, feature)
    profile = next((profile for profile in FACTOR_WEIGHT_PROFILES if profile.matches(context)), None)
    return profile.label if profile else DEFAULT_FACTOR_PROFILE, {}, [DEFAULT_FACTOR_WEIGHT_NOTE]


def _adjusted_factor_weight(factor_id: str, base_weight: float, adjustments: dict[str, float]) -> float:
    multiplier = adjustments.get(factor_id, 1.0)
    return round(max(WEIGHT_MULTIPLIER_MIN, min(WEIGHT_MULTIPLIER_MAX, base_weight * multiplier)), 2)


def _factor_weight_context(analysis: AnalysisResult, feature: FeatureSnapshot) -> FactorWeightContext:
    return FactorWeightContext(
        amount=feature.amount or 0,
        market_cap=analysis.quote.market_cap or 0,
        turnover=feature.turnover_rate,
        volume_ratio=feature.volume_ratio if feature.volume_ratio_available else 0.0,
    )


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
