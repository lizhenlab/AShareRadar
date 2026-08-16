from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.models.research import (
    FactorLabReport,
    MarketRegimeReport,
)
from app.models.analysis import (
    FeatureSnapshot,
)
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


@dataclass(frozen=True)
class PublishedRiskRewardLevels:
    upside_target: float
    downside_stop: float
    upside_pct: float
    downside_pct: float
    upside_available: bool
    downside_available: bool
    upside_target_basis: str
    downside_stop_basis: str


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
    levels = _published_risk_reward_levels(
        feature,
        factor_lab,
        market_regime,
        price,
        build_upside_target,
        build_downside_stop,
    )
    ratio = _reward_risk_ratio(levels.upside_pct, levels.downside_pct)
    ratio_available = levels.upside_available and levels.downside_available and ratio > 0
    atr14, atr_pct, volatility_pct = _available_risk_metrics(feature)
    return RiskRewardMetrics(
        price=price,
        upside_target=levels.upside_target,
        downside_stop=levels.downside_stop,
        upside_pct=levels.upside_pct,
        downside_pct=levels.downside_pct,
        ratio=ratio,
        atr14=atr14,
        atr_pct=atr_pct,
        volatility_pct=volatility_pct,
        upside_available=levels.upside_available,
        downside_available=levels.downside_available,
        ratio_available=ratio_available,
        upside_target_basis=levels.upside_target_basis,
        downside_stop_basis=levels.downside_stop_basis,
        availability_reason=_risk_reward_availability_reason(levels.upside_available, levels.downside_available),
    )


def _published_risk_reward_levels(
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    price: float,
    build_upside_target: UpsideTargetBuilder,
    build_downside_stop: DownsideStopBuilder,
) -> PublishedRiskRewardLevels:
    upside_basis = _upside_target_basis(feature, price)
    downside_basis = _downside_stop_basis(feature, price)
    upside_target = round(
        _upside_level_or_zero(build_upside_target(feature, factor_lab) if upside_basis != "unavailable" else 0, price),
        2,
    )
    downside_stop = round(
        _downside_level_or_zero(build_downside_stop(feature, market_regime) if downside_basis != "unavailable" else 0, price),
        2,
    )
    published_price = round(price, 2)
    upside_pct = round(_upside_distance_pct(upside_target, published_price), 2)
    downside_pct = round(_downside_distance_pct(downside_stop, published_price), 2)
    upside_available = upside_target > published_price > 0 and upside_pct > 0
    downside_available = 0 < downside_stop < published_price and downside_pct > 0
    return PublishedRiskRewardLevels(
        upside_target=upside_target if upside_available else 0,
        downside_stop=downside_stop if downside_available else 0,
        upside_pct=upside_pct if upside_available else 0,
        downside_pct=downside_pct if downside_available else 0,
        upside_available=upside_available,
        downside_available=downside_available,
        upside_target_basis=upside_basis if upside_available else "unavailable",
        downside_stop_basis=downside_basis if downside_available else "unavailable",
    )


def _available_risk_metrics(feature: FeatureSnapshot) -> tuple[float, float, float]:
    atr_available = getattr(feature, "atr14_available", False) is True
    volatility_available = getattr(feature, "volatility_available", False) is True
    return (
        _non_negative_or_zero(getattr(feature, "atr14", None)) if atr_available else 0,
        _non_negative_or_zero(getattr(feature, "atr_pct", None)) if atr_available else 0,
        _non_negative_or_zero(getattr(feature, "volatility_pct", None)) if volatility_available else 0,
    )


def _safe_pct_change(new_value: object, base_value: object) -> float:
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
    atr_available = _available_positive(feature, "atr14_available", "atr14")
    resistance_available = _available_upside_level(feature, "resistance_available", "resistance", price)
    atr14 = _non_negative_or_zero(getattr(feature, "atr14", None)) if atr_available else 0
    resistance = _positive_or_zero(getattr(feature, "resistance", None)) if resistance_available else 0
    candidates = [value for value in (resistance, price + atr14 * 1.35 if atr14 > 0 else 0) if value > price]
    if not candidates:
        return 0
    base_target = max(candidates)
    factor_score = _score_or_zero(getattr(factor_lab, "total_score", None))
    positive_count = _non_negative_or_zero(getattr(factor_lab, "positive_factor_count", 0))
    negative_count = _non_negative_or_zero(getattr(factor_lab, "negative_factor_count", 0))
    if atr14 > 0 and factor_score >= 65 and positive_count >= negative_count + 1:
        target = max(base_target, price + atr14 * 2.1)
        return _cap_upside_target(feature, target, price)
    if atr14 > 0 and factor_score <= 45:
        target = min(base_target, price + atr14 * 1.1)
        return _cap_upside_target(feature, target, price)
    return _cap_upside_target(feature, base_target, price)


def _cap_upside_target(feature: FeatureSnapshot, target: float, price: float) -> float:
    has_volatility_evidence = (
        getattr(feature, "atr14_available", False) is True
        or getattr(feature, "volatility_available", False) is True
    )
    return min(target, _upside_target_cap(feature, price)) if has_volatility_evidence else target


def _upside_target_cap(feature: FeatureSnapshot, price: float) -> float:
    atr_pct = min(
        _non_negative_or_zero(getattr(feature, "atr_pct", None))
        if getattr(feature, "atr14_available", False) is True
        else 0,
        UPSIDE_TARGET_ATR_PCT_CAP,
    )
    volatility_pct = min(
        _non_negative_or_zero(getattr(feature, "volatility_pct", None))
        if getattr(feature, "volatility_available", False) is True
        else 0,
        UPSIDE_TARGET_VOLATILITY_PCT_CAP,
    )
    cap_pct = max(UPSIDE_TARGET_MIN_CAP_PCT, (atr_pct * 1.8 + volatility_pct) / 100)
    cap_pct = min(cap_pct, UPSIDE_TARGET_MAX_CAP_PCT)
    return price * (1 + cap_pct)


def _downside_stop(feature: FeatureSnapshot, market_regime: MarketRegimeReport) -> float:
    context = _downside_stop_context(feature, market_regime)
    if context.price <= 0:
        return 0
    candidates: list[float] = []
    structural_stop = _structural_stop(context, feature)
    if structural_stop > 0:
        candidates.append(structural_stop)
    volatility_stop = _volatility_stop(context, feature)
    if volatility_stop > 0:
        candidates.append(volatility_stop)
    if not candidates:
        return 0
    raw_stop = min(candidates)
    lower_bound, upper_bound = _downside_stop_bounds(context)
    return min(max(raw_stop, lower_bound), upper_bound)


def _downside_stop_context(feature: FeatureSnapshot, market_regime: MarketRegimeReport) -> DownsideStopContext:
    return DownsideStopContext(
        price=_positive_or_zero(getattr(feature, "price", None)),
        support=(
            _positive_or_zero(getattr(feature, "support", None))
            if getattr(feature, "support_available", False) is True
            else 0
        ),
        ma20=(
            _positive_or_zero(getattr(feature, "ma20", None))
            if getattr(feature, "ma20_available", False) is True
            else 0
        ),
        atr14=(
            _non_negative_or_zero(getattr(feature, "atr14", None))
            if getattr(feature, "atr14_available", False) is True
            else 0
        ),
        atr_pct=(
            _non_negative_or_zero(getattr(feature, "atr_pct", None))
            if getattr(feature, "atr14_available", False) is True
            else 0
        ),
        volatility_pct=(
            _non_negative_or_zero(getattr(feature, "volatility_pct", None))
            if getattr(feature, "volatility_available", False) is True
            else 0
        ),
        risk_multiplier=_positive_or_one(getattr(market_regime, "risk_multiplier", None)),
    )


def _structural_stop(context: DownsideStopContext, feature: FeatureSnapshot) -> float:
    candidates = [
        level
        for level, available in (
            (context.support, getattr(feature, "support_available", False) is True),
            (context.ma20, getattr(feature, "ma20_available", False) is True),
        )
        if available and _is_usable_structural_stop(context, level)
    ]
    return min(candidates) if candidates else 0


def _is_usable_structural_stop(context: DownsideStopContext, level: float) -> bool:
    lower_bound = context.price * (1 - STRUCTURAL_STOP_MAX_DISTANCE_PCT)
    return lower_bound <= level < context.price


def _volatility_stop(context: DownsideStopContext, feature: FeatureSnapshot) -> float:
    if not _available_positive(feature, "atr14_available", "atr14"):
        return 0
    return context.price - context.atr14 * 1.15


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


def _upside_target_basis(feature: FeatureSnapshot, price: float) -> str:
    resistance = _available_upside_level(feature, "resistance_available", "resistance", price)
    atr = _available_positive(feature, "atr14_available", "atr14")
    if resistance and atr:
        return "resistance_and_atr"
    if resistance:
        return "resistance"
    if atr:
        return "atr"
    return "unavailable"


def _downside_stop_basis(feature: FeatureSnapshot, price: float) -> str:
    support = _available_downside_level(feature, "support_available", "support", price)
    ma20 = _available_downside_level(feature, "ma20_available", "ma20", price)
    structure = support or ma20
    atr = _available_positive(feature, "atr14_available", "atr14")
    if structure and atr:
        return "structure_and_atr"
    if structure:
        return "structure"
    if atr:
        return "atr"
    return "unavailable"


def _available_positive(feature: FeatureSnapshot, flag_name: str, value_name: str) -> bool:
    return getattr(feature, flag_name, False) is True and _positive_or_zero(getattr(feature, value_name, None)) > 0


def _available_upside_level(feature: FeatureSnapshot, flag_name: str, value_name: str, price: float) -> bool:
    return price > 0 and getattr(feature, flag_name, False) is True and _positive_or_zero(getattr(feature, value_name, None)) > price


def _available_downside_level(feature: FeatureSnapshot, flag_name: str, value_name: str, price: float) -> bool:
    value = _positive_or_zero(getattr(feature, value_name, None))
    return price > 0 and getattr(feature, flag_name, False) is True and 0 < value < price


def _risk_reward_availability_reason(upside_available: bool, downside_available: bool) -> str | None:
    if upside_available and downside_available:
        return None
    missing = []
    if not upside_available:
        missing.append("上方目标缺少压力位或ATR证据")
    if not downside_available:
        missing.append("下方防守缺少支撑、20日线或ATR证据")
    return "；".join(missing)


def _has_wide_volatility(context: DownsideStopContext) -> bool:
    return context.volatility_pct >= 4


def _has_wide_atr(context: DownsideStopContext) -> bool:
    return context.atr_pct >= 3.2


DOWNSIDE_STOP_ADJUSTMENT_RULES = (
    DownsideStopAdjustmentRule("wide_volatility", 0.012, _has_wide_volatility),
    DownsideStopAdjustmentRule("wide_atr", 0.01, _has_wide_atr),
)
