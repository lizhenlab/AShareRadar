from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from app.models.schemas import (
    AnalysisResult,
    FactorLabReport,
    FeatureSnapshot,
    MarketRegimeReport,
    RiskRewardReport,
    ScenarioPlan,
    SignalValidationReport,
    TimeframeAlignmentReport,
)
from app.services.indicators import pct_change
from app.services.scoring import clamp_score as _clamp
from app.utils.market_data import finite_float


CONFIRMING_VALIDATION_STATUSES = {"条件较好", "等待二次确认"}
TIMEFRAME_WAIT_LEVELS = {"中冲突", "多周期偏弱"}
SCENARIO_MIN_NEUTRAL_PROBABILITY = 10
SCENARIO_POSITIVE_BASE = 32
SCENARIO_RISK_BASE = 28
SCENARIO_FACTOR_WEIGHT = 0.25
SCENARIO_RISK_MULTIPLIER_WEIGHT = 18
SCENARIO_CYCLE_CONFLICT_POSITIVE_CAP = 18
SCENARIO_RISK_PRIORITY_POSITIVE_CAP = 12
SCENARIO_WAIT_CONFIRM_POSITIVE_CAP = 30
DOWNSIDE_MIN_LOSS_PCT = 0.018
DOWNSIDE_NORMAL_BASE_LOSS_PCT = 0.075
DOWNSIDE_HIGH_RISK_BASE_LOSS_PCT = 0.055
DOWNSIDE_HIGH_RISK_MULTIPLIER = 1.18
STRUCTURAL_STOP_MAX_DISTANCE_PCT = 0.12
UPSIDE_TARGET_MIN_CAP_PCT = 0.08
UPSIDE_TARGET_MAX_CAP_PCT = 0.22
UPSIDE_TARGET_ATR_PCT_CAP = 10
UPSIDE_TARGET_VOLATILITY_PCT_CAP = 12


@dataclass(frozen=True)
class RiskRewardRatingContext:
    ratio: float
    factor_score: int
    risk_multiplier: float
    breadth_score: int
    validation_status: str
    timeframe_conflict: str | None


@dataclass(frozen=True)
class RiskRewardRatingRule:
    name: str
    rating: str
    matches: Callable[[RiskRewardRatingContext], bool]


@dataclass(frozen=True)
class DownsideStopContext:
    price: float
    support: float
    ma20: float
    atr14: float
    atr_pct: float
    volatility_pct: float
    risk_multiplier: float


@dataclass(frozen=True)
class DownsideStopAdjustmentRule:
    name: str
    adjustment: float
    matches: Callable[[DownsideStopContext], bool]


@dataclass(frozen=True)
class ScenarioProbabilities:
    positive: int
    neutral: int
    risk: int


@dataclass(frozen=True)
class RiskRewardMetrics:
    price: float
    upside_target: float
    downside_stop: float
    upside_pct: float
    downside_pct: float
    ratio: float
    atr14: float
    atr_pct: float
    volatility_pct: float


@dataclass(frozen=True)
class RiskRewardReportParts:
    metrics: RiskRewardMetrics
    rating: str
    summary: str
    scenarios: list[ScenarioPlan]
    notes: list[str]


@dataclass(frozen=True)
class RiskRewardLevelAvailability:
    price_available: bool
    upside_available: bool
    downside_available: bool
    ratio_available: bool


@dataclass(frozen=True)
class ScenarioPlanContext:
    price: float
    probabilities: ScenarioProbabilities
    validation_status: str
    action: str
    support: float
    resistance: float
    ma20: float
    upside_target: float
    downside_stop: float


def build_risk_reward_report(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> RiskRewardReport:
    parts = _risk_reward_report_parts(analysis, feature, factor_lab, market_regime, validation, timeframe)
    return _risk_reward_report_from_parts(feature, parts)


def _risk_reward_report_parts(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    timeframe: TimeframeAlignmentReport | None,
) -> RiskRewardReportParts:
    metrics = _risk_reward_metrics(feature, factor_lab, market_regime)
    rating = _risk_reward_rating(metrics.ratio, factor_lab, market_regime, validation, timeframe)
    scenarios = _scenario_plans(
        analysis,
        feature,
        factor_lab,
        market_regime,
        validation,
        metrics.upside_target,
        metrics.downside_stop,
        rating=rating,
        timeframe=timeframe,
    )
    summary = _risk_reward_summary(
        rating,
        metrics.ratio,
        metrics.upside_pct,
        metrics.downside_pct,
        market_regime,
        timeframe,
        feature,
        metrics.upside_target,
        metrics.downside_stop,
    )
    return RiskRewardReportParts(
        metrics=metrics,
        rating=rating,
        summary=summary,
        scenarios=scenarios,
        notes=_risk_reward_notes(metrics),
    )


def _risk_reward_report_from_parts(feature: FeatureSnapshot, parts: RiskRewardReportParts) -> RiskRewardReport:
    metrics = parts.metrics
    return RiskRewardReport(
        symbol=_text_or_default(getattr(feature, "symbol", None), ""),
        updated_at=_text_or_default(getattr(feature, "updated_at", None), ""),
        current_price=round(metrics.price, 2),
        upside_target=round(metrics.upside_target, 2),
        downside_stop=round(metrics.downside_stop, 2),
        upside_pct=round(metrics.upside_pct, 2),
        downside_pct=round(metrics.downside_pct, 2),
        reward_risk_ratio=metrics.ratio,
        atr14=round(metrics.atr14, 2),
        atr_pct=round(metrics.atr_pct, 2),
        volatility_pct=round(metrics.volatility_pct, 2),
        rating=parts.rating,
        summary=parts.summary,
        scenarios=parts.scenarios,
        notes=parts.notes,
    )


def _risk_reward_metrics(
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
) -> RiskRewardMetrics:
    price = _positive_or_zero(getattr(feature, "price", None))
    upside_target = _upside_level_or_zero(_upside_target(feature, factor_lab), price)
    downside_stop = _downside_level_or_zero(_downside_stop(feature, market_regime), price)
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


def _positive_or_zero(value: object) -> float:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else 0


def _non_negative_or_zero(value: object) -> float:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed >= 0 else 0


def _positive_or_one(value: object) -> float:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else 1


def _score_or_zero(value: object) -> int:
    return _clamp(value)


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


def _risk_reward_rating(
    ratio: float,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> str:
    context = _rating_context(ratio, factor_lab, market_regime, validation, timeframe)
    for rule in RISK_REWARD_RATING_RULES:
        if rule.matches(context):
            return rule.rating
    return "性价比不足"


def _rating_context(
    ratio: float,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    timeframe: TimeframeAlignmentReport | None,
) -> RiskRewardRatingContext:
    return RiskRewardRatingContext(
        ratio=_non_negative_or_zero(ratio),
        factor_score=_score_or_zero(getattr(factor_lab, "total_score", None)),
        risk_multiplier=_positive_or_one(getattr(market_regime, "risk_multiplier", None)),
        breadth_score=_score_or_zero(getattr(market_regime, "breadth_score", None)),
        validation_status=_validation_status_text(validation),
        timeframe_conflict=_text_or_default(getattr(timeframe, "conflict_level", None), "") if timeframe else None,
    )


def _has_high_timeframe_conflict(context: RiskRewardRatingContext) -> bool:
    return context.timeframe_conflict == "高冲突"


def _has_low_ratio_timeframe_conflict(context: RiskRewardRatingContext) -> bool:
    return context.timeframe_conflict in TIMEFRAME_WAIT_LEVELS and context.ratio < 1.35


def _has_external_risk_priority(context: RiskRewardRatingContext) -> bool:
    return context.risk_multiplier >= 1.28 or context.validation_status == "风险优先"


def _has_attractive_risk_reward(context: RiskRewardRatingContext) -> bool:
    return (
        context.ratio >= 1.8
        and context.factor_score >= 58
        and context.validation_status in CONFIRMING_VALIDATION_STATUSES
        and context.breadth_score >= 42
    )


def _needs_timeframe_confirmation(context: RiskRewardRatingContext) -> bool:
    return context.ratio >= 1.35 and context.timeframe_conflict in TIMEFRAME_WAIT_LEVELS


def _has_acceptable_risk_reward(context: RiskRewardRatingContext) -> bool:
    return context.ratio >= 1.2


RISK_REWARD_RATING_RULES = (
    RiskRewardRatingRule("high_timeframe_conflict", "周期冲突", _has_high_timeframe_conflict),
    RiskRewardRatingRule("low_ratio_timeframe_conflict", "周期冲突", _has_low_ratio_timeframe_conflict),
    RiskRewardRatingRule("external_risk_priority", "风险优先", _has_external_risk_priority),
    RiskRewardRatingRule("attractive_risk_reward", "性价比较好", _has_attractive_risk_reward),
    RiskRewardRatingRule("timeframe_confirmation", "等待确认", _needs_timeframe_confirmation),
    RiskRewardRatingRule("acceptable_risk_reward", "性价比一般", _has_acceptable_risk_reward),
)


def _risk_reward_summary(
    rating: str,
    ratio: float,
    upside_pct: float,
    downside_pct: float,
    market_regime: MarketRegimeReport,
    timeframe: TimeframeAlignmentReport | None = None,
    feature: FeatureSnapshot | None = None,
    upside_target: object | None = None,
    downside_stop: object | None = None,
) -> str:
    timeframe_text = _timeframe_summary_text(timeframe)
    volatility_text = ""
    if feature:
        atr_text = _non_negative_percent_text("ATR", getattr(feature, "atr_pct", None))
        volatility_pct_text = _non_negative_percent_text("20日波动", getattr(feature, "volatility_pct", None))
        volatility_text = f"{atr_text}、{volatility_pct_text}；"
    availability = _risk_reward_level_availability(
        feature=feature,
        upside_target=upside_target,
        downside_stop=downside_stop,
        upside_pct=upside_pct,
        downside_pct=downside_pct,
        ratio=ratio,
    )
    return (
        f"{rating}：{timeframe_text}{volatility_text}"
        f"{_conditional_percent_text('上方预估空间', upside_pct, availability.upside_available)}，"
        f"{_conditional_percent_text('下方防守距离', downside_pct, availability.downside_available)}，"
        f"{_ratio_text(ratio, availability.ratio_available)}；"
        f"{_risk_multiplier_text(getattr(market_regime, 'risk_multiplier', None))}。"
    )


def _risk_reward_level_availability(
    *,
    feature: FeatureSnapshot | None,
    upside_target: object | None,
    downside_stop: object | None,
    upside_pct: object,
    downside_pct: object,
    ratio: object,
) -> RiskRewardLevelAvailability:
    price = _positive_or_zero(getattr(feature, "price", None)) if feature else 0
    price_available = feature is None or price > 0
    upside_available = (
        price_available and _upside_level_available(upside_target, price) and _non_negative_or_zero(upside_pct) > 0
    )
    downside_available = (
        price_available and _downside_level_available(downside_stop, price) and _non_negative_or_zero(downside_pct) > 0
    )
    ratio_available = upside_available and downside_available and _non_negative_or_zero(ratio) > 0
    return RiskRewardLevelAvailability(
        price_available=price_available,
        upside_available=upside_available,
        downside_available=downside_available,
        ratio_available=ratio_available,
    )


def _risk_reward_notes(metrics: RiskRewardMetrics) -> list[str]:
    notes = ["风险收益比只用于单股观察，不代表收益承诺。"]
    if not _metric_levels_are_valid(metrics):
        notes.append("当前价格、目标位或防守位存在待确认项，先按观察口径处理。")
    else:
        notes.append(
            "目标位和防守位已参考ATR和近期波动率；"
            "若数据质量或市场环境恶化，应优先使用下方失效位。"
        )
    return notes


def _metric_levels_are_valid(metrics: RiskRewardMetrics) -> bool:
    return metrics.price > 0 and metrics.upside_target > metrics.price and 0 < metrics.downside_stop < metrics.price


def _timeframe_summary_text(timeframe: TimeframeAlignmentReport | None) -> str:
    if not timeframe:
        return ""
    label = _text_or_default(getattr(timeframe, "alignment_label", None), "待确认")
    return f"多周期「{label}」；"


def _non_negative_percent_text(label: str, value: object) -> str:
    parsed = finite_float(value)
    if parsed is not None and parsed >= 0:
        return f"{label} {parsed:.2f}%"
    return f"{label}待确认"


def _conditional_percent_text(label: str, value: object, available: bool) -> str:
    parsed = finite_float(value)
    if available and parsed is not None and parsed >= 0:
        return f"{label} {parsed:.2f}%"
    return f"{label}待确认"


def _ratio_text(value: object, available: bool) -> str:
    parsed = finite_float(value)
    if available and parsed is not None and parsed > 0:
        return f"收益风险比 {parsed:.2f}"
    return "收益风险比待确认"


def _risk_multiplier_text(value: object) -> str:
    parsed = finite_float(value)
    if parsed is not None and parsed > 0:
        return f"环境风险倍率 {parsed:.2f}"
    return "环境风险倍率待确认"


def _upside_level_available(value: object | None, price: float) -> bool:
    return _upside_level_or_zero(value, price) > 0


def _downside_level_available(value: object | None, price: float) -> bool:
    return _downside_level_or_zero(value, price) > 0


def _upside_level_or_zero(value: object | None, price: float) -> float:
    level = _positive_or_zero(value)
    return level if level > 0 and (price <= 0 or level > price) else 0


def _downside_level_or_zero(value: object | None, price: float) -> float:
    level = _positive_or_zero(value)
    return level if level > 0 and (price <= 0 or level < price) else 0


def _scenario_plans(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    upside_target: float,
    downside_stop: float,
    *,
    rating: str | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> list[ScenarioPlan]:
    context = _scenario_plan_context(
        analysis,
        feature,
        factor_lab,
        market_regime,
        validation,
        upside_target,
        downside_stop,
        rating,
        timeframe,
    )
    return [_positive_scenario_plan(context), _neutral_scenario_plan(context), _defensive_scenario_plan(context)]


def _positive_scenario_plan(context: ScenarioPlanContext) -> ScenarioPlan:
    return ScenarioPlan(
        name="积极路径",
        probability=context.probabilities.positive,
        trigger=_positive_scenario_trigger(context),
        expected_move=_positive_scenario_expected_move(context),
        response="只在确认后提高关注度，避免盘中追高。",
        invalidation=_positive_scenario_invalidation(context),
    )


def _neutral_scenario_plan(context: ScenarioPlanContext) -> ScenarioPlan:
    return ScenarioPlan(
        name="震荡路径",
        probability=context.probabilities.neutral,
        trigger=_neutral_scenario_trigger(context),
        expected_move="以支撑、压力和量能变化为主，不提前给方向结论。",
        response="适合观察或仅底仓做T，新增动作等待确认。",
        invalidation="区间被放量跌破或放量突破。",
    )


def _defensive_scenario_plan(context: ScenarioPlanContext) -> ScenarioPlan:
    return ScenarioPlan(
        name="防守路径",
        probability=context.probabilities.risk,
        trigger=_defensive_scenario_trigger(context),
        expected_move="优先看风险释放，不急于判断反转。",
        response=f"维持「{context.action}」口径，先处理风控线。",
        invalidation="重新站回5日线且量能、资金同步修复。",
    )


def _scenario_plan_context(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    upside_target: float,
    downside_stop: float,
    rating: str | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> ScenarioPlanContext:
    price = _positive_or_zero(getattr(feature, "price", None))
    support = _scenario_support_level(getattr(feature, "support", None), price)
    resistance = _scenario_resistance_level(getattr(feature, "resistance", None), price)
    ma20 = _positive_or_zero(getattr(feature, "ma20", None))
    clean_upside_target = _scenario_resistance_level(upside_target, price)
    clean_downside_stop = _scenario_support_level(downside_stop, price)
    validation_status = _validation_status_text(validation)
    return ScenarioPlanContext(
        price=price,
        probabilities=_scenario_probabilities_for_context(
            factor_lab,
            market_regime,
            price=price,
            support=support,
            resistance=resistance,
            upside_target=clean_upside_target,
            downside_stop=clean_downside_stop,
            rating=rating,
            validation_status=validation_status,
            timeframe_conflict=_timeframe_conflict_text(timeframe),
        ),
        validation_status=validation_status,
        action=_analysis_action_text(analysis),
        support=support,
        resistance=resistance,
        ma20=ma20,
        upside_target=clean_upside_target,
        downside_stop=clean_downside_stop,
    )


def _scenario_probabilities_for_context(
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    *,
    price: float,
    support: float,
    resistance: float,
    upside_target: float,
    downside_stop: float,
    rating: str | None,
    validation_status: str,
    timeframe_conflict: str,
) -> ScenarioProbabilities:
    level_probabilities = _adjust_scenario_probabilities_for_levels(
        _scenario_probabilities(
            getattr(factor_lab, "total_score", None),
            getattr(market_regime, "risk_multiplier", None),
        ),
        price=price,
        support=support,
        resistance=resistance,
        upside_target=upside_target,
        downside_stop=downside_stop,
    )
    return _adjust_scenario_probabilities_for_decision_state(
        level_probabilities,
        rating=rating,
        validation_status=validation_status,
        timeframe_conflict=timeframe_conflict,
    )


def _scenario_support_level(value: object, price: float) -> float:
    if price <= 0:
        return 0
    return _downside_level_or_zero(value, price)


def _scenario_resistance_level(value: object, price: float) -> float:
    if price <= 0:
        return 0
    return _upside_level_or_zero(value, price)


def _validation_status_text(validation: SignalValidationReport) -> str:
    return _text_or_default(getattr(validation, "overall_status", None), "等待确认")


def _analysis_action_text(analysis: AnalysisResult) -> str:
    action_advice = getattr(analysis, "action_advice", None)
    return _text_or_default(getattr(action_advice, "action", None), "观察")


def _timeframe_conflict_text(timeframe: TimeframeAlignmentReport | None) -> str:
    return _text_or_default(getattr(timeframe, "conflict_level", None), "") if timeframe else ""


def _text_or_default(value: object, default: str) -> str:
    if _is_non_finite_text_value(value):
        return default
    text = str(value or "").strip()
    return text or default


def _is_non_finite_text_value(value: object) -> bool:
    text = str(value).strip().lower()
    return text in {"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}


def _positive_scenario_trigger(context: ScenarioPlanContext) -> str:
    if context.price <= 0:
        return (
            f"当前价待确认，暂不设置积极突破触发；"
            f"验证状态需维持在「{context.validation_status}」或更好。"
        )
    if context.resistance <= 0:
        return (
            f"压力位待确认，需先形成可验证突破边界，"
            f"且验证状态维持在「{context.validation_status}」或更好。"
        )
    resistance = _labelled_price_level("压力位", context.resistance)
    return f"放量站稳{resistance}，且验证状态维持在「{context.validation_status}」或更好。"


def _positive_scenario_expected_move(context: ScenarioPlanContext) -> str:
    if context.upside_target > 0:
        return f"先看 {context.upside_target:.2f} 附近，若继续放量再重新评估。"
    return "先等上方目标确认，若继续放量再重新评估。"


def _positive_scenario_invalidation(context: ScenarioPlanContext) -> str:
    if context.price <= 0:
        return "当前价或压力位无法复核，积极路径暂不成立。"
    if context.resistance > 0:
        return f"突破后跌回 {context.resistance:.2f} 下方。"
    return "突破后压力位仍未确认，或放量无法延续。"


def _neutral_scenario_trigger(context: ScenarioPlanContext) -> str:
    if context.price <= 0:
        return "当前价待确认，先按支撑、压力复核后的震荡观察处理。"
    if context.support > 0 and context.resistance > 0:
        return f"价格继续在 {context.support:.2f} 到 {context.resistance:.2f} 区间内波动。"
    if context.support > 0:
        return f"价格继续在 {context.support:.2f} 上方震荡，压力位仍待确认。"
    if context.resistance > 0:
        return f"价格在支撑位待确认、{context.resistance:.2f} 下方震荡。"
    return "支撑和压力位仍待确认，价格延续无明确边界的震荡。"


def _defensive_scenario_trigger(context: ScenarioPlanContext) -> str:
    if context.price <= 0:
        downside_text = "当前价或防守位待确认"
    else:
        downside_text = (
            f"有效跌破 {context.downside_stop:.2f}"
            if context.downside_stop > 0
            else "防守位待确认"
        )
    ma20_text = (
        f"20日线 {context.ma20:.2f} 下方不能修复"
        if context.ma20 > 0
        else "20日线待确认且弱势延续"
    )
    return f"{downside_text}，或{ma20_text}。"


def _labelled_price_level(label: str, value: float) -> str:
    return f"{label} {value:.2f}" if value > 0 else f"{label}待确认"


def _scenario_probabilities(factor_score: object, risk_multiplier: object) -> ScenarioProbabilities:
    score = _score_or_zero(factor_score)
    multiplier = _positive_or_one(risk_multiplier)
    positive = _clamp(
        round(
            SCENARIO_POSITIVE_BASE
            + score * SCENARIO_FACTOR_WEIGHT
            + max(0, 1.1 - multiplier) * SCENARIO_RISK_MULTIPLIER_WEIGHT
        )
    )
    risk = _clamp(
        round(
            SCENARIO_RISK_BASE
            + multiplier * SCENARIO_RISK_MULTIPLIER_WEIGHT
            + max(0, 50 - score) * SCENARIO_FACTOR_WEIGHT
        )
    )
    neutral = max(SCENARIO_MIN_NEUTRAL_PROBABILITY, 100 - positive - risk)
    return _normalize_scenario_probabilities(positive, neutral, risk)


def _adjust_scenario_probabilities_for_levels(
    probabilities: ScenarioProbabilities,
    *,
    price: float,
    support: float,
    resistance: float,
    upside_target: float,
    downside_stop: float,
) -> ScenarioProbabilities:
    positive = probabilities.positive
    neutral = probabilities.neutral
    risk = probabilities.risk
    if price <= 0:
        moved = positive
        positive = 0
        neutral += moved // 2
        risk += moved - moved // 2
        return _normalize_scenario_probabilities(positive, neutral, risk)
    if resistance <= 0:
        positive, neutral = _shift_probability(positive, neutral, 8)
    if upside_target <= 0:
        positive, neutral = _shift_probability(positive, neutral, 10)
    if support <= 0:
        positive, risk = _shift_probability(positive, risk, 6)
    if downside_stop <= 0:
        positive, risk = _shift_probability(positive, risk, 10)
    return _normalize_scenario_probabilities(positive, neutral, risk)


def _adjust_scenario_probabilities_for_decision_state(
    probabilities: ScenarioProbabilities,
    *,
    rating: str | None,
    validation_status: str,
    timeframe_conflict: str,
) -> ScenarioProbabilities:
    if rating == "风险优先" or validation_status == "风险优先":
        return _cap_positive_probability(probabilities, SCENARIO_RISK_PRIORITY_POSITIVE_CAP, neutral_share=0.25)
    if rating == "周期冲突" or timeframe_conflict == "高冲突":
        return _cap_positive_probability(probabilities, SCENARIO_CYCLE_CONFLICT_POSITIVE_CAP, neutral_share=0.65)
    if rating == "等待确认" or timeframe_conflict in TIMEFRAME_WAIT_LEVELS:
        return _cap_positive_probability(probabilities, SCENARIO_WAIT_CONFIRM_POSITIVE_CAP, neutral_share=1.0)
    if validation_status not in CONFIRMING_VALIDATION_STATUSES:
        return _cap_positive_probability(probabilities, SCENARIO_WAIT_CONFIRM_POSITIVE_CAP, neutral_share=1.0)
    return probabilities


def _cap_positive_probability(
    probabilities: ScenarioProbabilities,
    cap: int,
    *,
    neutral_share: float,
) -> ScenarioProbabilities:
    positive = probabilities.positive
    neutral = probabilities.neutral
    risk = probabilities.risk
    excess = max(0, positive - max(0, cap))
    if excess <= 0:
        return probabilities
    positive -= excess
    neutral_move = round(excess * max(0, min(1, neutral_share)))
    neutral += neutral_move
    risk += excess - neutral_move
    return _normalize_scenario_probabilities(positive, neutral, risk)


def _shift_probability(source: int, destination: int, amount: int) -> tuple[int, int]:
    moved = min(max(0, source), max(0, amount))
    return source - moved, destination + moved


def _normalize_scenario_probabilities(positive: object, neutral: object, risk: object) -> ScenarioProbabilities:
    positive = _probability_or_zero(positive)
    neutral = _probability_or_zero(neutral)
    risk = _probability_or_zero(risk)
    total = positive + neutral + risk
    if total <= 0:
        return ScenarioProbabilities(positive=0, neutral=100, risk=0)
    normalized_positive, normalized_neutral, normalized_risk = _integer_probability_split(
        (positive, neutral, risk),
        100,
    )
    if normalized_neutral < SCENARIO_MIN_NEUTRAL_PROBABILITY:
        return _reserve_neutral_probability(normalized_positive, normalized_risk)
    return ScenarioProbabilities(
        positive=normalized_positive,
        neutral=normalized_neutral,
        risk=normalized_risk,
    )


def _reserve_neutral_probability(positive: int, risk: int) -> ScenarioProbabilities:
    positive = _probability_or_zero(positive)
    risk = _probability_or_zero(risk)
    directional_total = positive + risk
    if directional_total <= 0:
        return ScenarioProbabilities(positive=0, neutral=100, risk=0)
    directional_budget = 100 - SCENARIO_MIN_NEUTRAL_PROBABILITY
    normalized_positive, normalized_risk = _integer_probability_split(
        (positive, risk),
        directional_budget,
    )
    return ScenarioProbabilities(
        positive=normalized_positive,
        neutral=SCENARIO_MIN_NEUTRAL_PROBABILITY,
        risk=normalized_risk,
    )


def _integer_probability_split(values: tuple[int, ...], budget: int) -> tuple[int, ...]:
    total = sum(values)
    if budget <= 0 or total <= 0:
        return tuple(0 for _ in values)
    exact = [value / total * budget for value in values]
    base = [math.floor(value) for value in exact]
    remainder = budget - sum(base)
    order = sorted(range(len(values)), key=lambda index: exact[index] - base[index], reverse=True)
    for index in order[:remainder]:
        base[index] += 1
    return tuple(base)


def _probability_or_zero(value: object) -> int:
    parsed = finite_float(value)
    if parsed is None or parsed <= 0:
        return 0
    return round(parsed)
