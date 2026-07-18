from __future__ import annotations

from collections.abc import Callable

from app.models.schemas import FactorLabReport, FeatureSnapshot, MarketRegimeReport
from app.services.indicators import pct_change
from app.services.research_risk_reward_contracts import (
    DOWNSIDE_HIGH_RISK_BASE_LOSS_PCT,
    DOWNSIDE_HIGH_RISK_MULTIPLIER,
    DOWNSIDE_MIN_LOSS_PCT,
    DOWNSIDE_NORMAL_BASE_LOSS_PCT,
    STRUCTURAL_STOP_MAX_DISTANCE_PCT,
    UPSIDE_TARGET_ATR_PCT_CAP,
    UPSIDE_TARGET_MAX_CAP_PCT,
    UPSIDE_TARGET_MIN_CAP_PCT,
    UPSIDE_TARGET_VOLATILITY_PCT_CAP,
    DownsideStopAdjustmentRule,
    DownsideStopContext,
    RiskRewardMetrics,
)
from app.services.research_risk_reward_values import (
    _downside_level_or_zero,
    _non_negative_or_zero,
    _positive_or_one,
    _positive_or_zero,
    _score_or_zero,
    _upside_level_or_zero,
)
from app.utils.market_data import finite_float


UpsideTargetBuilder = Callable[[FeatureSnapshot, FactorLabReport], float]
DownsideStopBuilder = Callable[[FeatureSnapshot, MarketRegimeReport], float]


def _risk_reward_metrics(
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    *,
    upside_target_builder: UpsideTargetBuilder | None = None,
    downside_stop_builder: DownsideStopBuilder | None = None,
) -> RiskRewardMetrics:
    price = _positive_or_zero(getattr(feature, "price", None))
    build_upside_target = upside_target_builder or _upside_target
    build_downside_stop = downside_stop_builder or _downside_stop
    upside_target = _upside_level_or_zero(build_upside_target(feature, factor_lab), price)
    downside_stop = _downside_level_or_zero(build_downside_stop(feature, market_regime), price)
    upside_pct = _upside_distance_pct(upside_target, price)
    downside_pct = _downside_distance_pct(downside_stop, price)
    return RiskRewardMetrics(
        price=price,
        upside_target=upside_target,
        downside_stop=downside_stop,
        upside_pct=upside_pct,
        downside_pct=downside_pct,
        ratio=_reward_risk_ratio(upside_pct, downside_pct),
        atr14=_non_negative_or_zero(getattr(feature, "atr14", None)),
        atr_pct=_non_negative_or_zero(getattr(feature, "atr_pct", None)),
        volatility_pct=_non_negative_or_zero(getattr(feature, "volatility_pct", None)),
    )


def _safe_pct_change(new_value: float, base_value: float) -> float:
    parsed_base = finite_float(base_value)
    parsed_new = finite_float(new_value)
    if parsed_base is None or parsed_base <= 0 or parsed_new is None:
        return 0
    change = finite_float(pct_change(parsed_new, parsed_base))
    return change if change is not None else 0


def _upside_distance_pct(upside_target: object, price: object) -> float:
    if _positive_or_zero(upside_target) <= 0 or _positive_or_zero(price) <= 0:
        return 0
    change = _safe_pct_change(upside_target, price)
    return change if change > 0 else 0


def _downside_distance_pct(downside_stop: object, price: object) -> float:
    if _positive_or_zero(downside_stop) <= 0 or _positive_or_zero(price) <= 0:
        return 0
    change = _safe_pct_change(downside_stop, price)
    return abs(change) if change < 0 else 0


def _reward_risk_ratio(upside_pct: float, downside_pct: float) -> float:
    upside = _non_negative_or_zero(upside_pct)
    downside = _non_negative_or_zero(downside_pct)
    if upside <= 0 or downside <= 0:
        return 0
    return round(upside / downside, 2)


def _upside_target(feature: FeatureSnapshot, factor_lab: FactorLabReport) -> float:
    price = _positive_or_zero(getattr(feature, "price", None))
    if price <= 0:
        return 0
    atr14 = _non_negative_or_zero(getattr(feature, "atr14", None))
    resistance = _positive_or_zero(getattr(feature, "resistance", None))
    volatility_target = price + max(atr14 * 1.35, price * 0.018)
    base_target = max(resistance, volatility_target, price * 1.025)
    factor_score = _score_or_zero(getattr(factor_lab, "total_score", None))
    positive_count = _non_negative_or_zero(getattr(factor_lab, "positive_factor_count", 0))
    negative_count = _non_negative_or_zero(getattr(factor_lab, "negative_factor_count", 0))
    if factor_score >= 65 and positive_count >= negative_count + 1:
        target = max(base_target, price + max(atr14 * 2.1, price * 0.04))
        return min(target, _upside_target_cap(feature, price))
    if factor_score <= 45:
        target = min(base_target, price + max(atr14 * 1.1, price * 0.022))
        return min(target, _upside_target_cap(feature, price))
    return min(base_target, _upside_target_cap(feature, price))


def _upside_target_cap(feature: FeatureSnapshot, price: float) -> float:
    atr_pct = min(_non_negative_or_zero(getattr(feature, "atr_pct", None)), UPSIDE_TARGET_ATR_PCT_CAP)
    volatility_pct = min(
        _non_negative_or_zero(getattr(feature, "volatility_pct", None)),
        UPSIDE_TARGET_VOLATILITY_PCT_CAP,
    )
    cap_pct = max(UPSIDE_TARGET_MIN_CAP_PCT, (atr_pct * 1.8 + volatility_pct) / 100)
    cap_pct = min(cap_pct, UPSIDE_TARGET_MAX_CAP_PCT)
    return price * (1 + cap_pct)


def _downside_stop(feature: FeatureSnapshot, market_regime: MarketRegimeReport) -> float:
    context = _downside_stop_context(feature, market_regime)
    if context.price <= 0:
        return 0
    raw_stop = min(_structural_stop(context), _volatility_stop(context))
    lower_bound, upper_bound = _downside_stop_bounds(context)
    return min(max(raw_stop, lower_bound), upper_bound)


def _downside_stop_context(feature: FeatureSnapshot, market_regime: MarketRegimeReport) -> DownsideStopContext:
    return DownsideStopContext(
        price=_positive_or_zero(getattr(feature, "price", None)),
        support=_positive_or_zero(getattr(feature, "support", None)),
        ma20=_positive_or_zero(getattr(feature, "ma20", None)),
        atr14=_non_negative_or_zero(getattr(feature, "atr14", None)),
        atr_pct=_non_negative_or_zero(getattr(feature, "atr_pct", None)),
        volatility_pct=_non_negative_or_zero(getattr(feature, "volatility_pct", None)),
        risk_multiplier=_positive_or_one(getattr(market_regime, "risk_multiplier", None)),
    )


def _structural_stop(context: DownsideStopContext) -> float:
    candidates = [item for item in [context.support, context.ma20] if _is_usable_structural_stop(context, item)]
    return min(candidates) if candidates else context.price * 0.97


def _is_usable_structural_stop(context: DownsideStopContext, level: float) -> bool:
    lower_bound = context.price * (1 - STRUCTURAL_STOP_MAX_DISTANCE_PCT)
    return lower_bound <= level < context.price


def _volatility_stop(context: DownsideStopContext) -> float:
    atr_buffer = max(context.atr14 * 1.15, context.price * 0.018)
    return context.price - atr_buffer


def _downside_stop_bounds(context: DownsideStopContext) -> tuple[float, float]:
    max_loss_pct = _max_loss_pct(context)
    return context.price * (1 - max_loss_pct), context.price * (1 - DOWNSIDE_MIN_LOSS_PCT)


def _max_loss_pct(context: DownsideStopContext) -> float:
    base = (
        DOWNSIDE_NORMAL_BASE_LOSS_PCT
        if context.risk_multiplier < DOWNSIDE_HIGH_RISK_MULTIPLIER
        else DOWNSIDE_HIGH_RISK_BASE_LOSS_PCT
    )
    adjustments = sum(rule.adjustment for rule in DOWNSIDE_STOP_ADJUSTMENT_RULES if rule.matches(context))
    return base + adjustments


def _has_wide_volatility(context: DownsideStopContext) -> bool:
    return context.volatility_pct >= 4


def _has_wide_atr(context: DownsideStopContext) -> bool:
    return context.atr_pct >= 3.2


DOWNSIDE_STOP_ADJUSTMENT_RULES = (
    DownsideStopAdjustmentRule("wide_volatility", 0.012, _has_wide_volatility),
    DownsideStopAdjustmentRule("wide_atr", 0.01, _has_wide_atr),
)
