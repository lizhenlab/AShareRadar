from __future__ import annotations

from app.models.schemas import FactorLabReport, FeatureSnapshot, MarketRegimeReport, SignalValidationReport, TimeframeAlignmentReport
from app.services.research_risk_reward_contracts import (
    CONFIRMING_VALIDATION_STATUSES,
    TIMEFRAME_WAIT_LEVELS,
    RiskRewardLevelAvailability,
    RiskRewardMetrics,
    RiskRewardRatingContext,
    RiskRewardRatingRule,
)
from app.services.research_risk_reward_values import (
    _downside_level_available,
    _non_negative_or_zero,
    _positive_or_one,
    _positive_or_zero,
    _score_or_zero,
    _text_or_default,
    _upside_level_available,
    _validation_status_text,
)
from app.utils.market_data import finite_float


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
