from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import (
    AnalysisResult,
    FactorLabReport,
    FeatureSnapshot,
    MarketRegimeReport,
    SignalValidationItem,
    SignalValidationReport,
    StandardFactor,
    TimeframeAlignmentReport,
)
from app.services.research_factors import _factor_reference, _find_factor
from app.services.scoring import clamp_score as _clamp
from app.utils.market_data import finite_float as _finite_float


TIMEFRAME_HIGH_CONFLICT = "高冲突"
TIMEFRAME_MEDIUM_CONFLICT = "中冲突"
TIMEFRAME_MULTI_WEAK = "多周期偏弱"
BLOCKING_TIMEFRAME_LEVELS = {TIMEFRAME_HIGH_CONFLICT, TIMEFRAME_MULTI_WEAK}
MIXED_TIMEFRAME_LEVELS = {TIMEFRAME_MEDIUM_CONFLICT, TIMEFRAME_MULTI_WEAK}
CONSERVATIVE_TIMEFRAME_LEVELS = BLOCKING_TIMEFRAME_LEVELS | {TIMEFRAME_MEDIUM_CONFLICT}
WEAK_FACTOR_EXPECTED_LEVELS = {"风险", "偏弱"}
ENVIRONMENT_RISK_MULTIPLIER_THRESHOLD = 1.28
LOW_RISK_MULTIPLIER_THRESHOLD = 1.08
VOLUME_CONFIRMATION_RATIO = 1.1
DEFAULT_RISK_MULTIPLIER = 1.0
DEFAULT_FACTOR_STABILITY_SCORE = 45
DEFAULT_CONFIDENCE_ADJUSTMENT = 0

STATUS_ENVIRONMENT_SUPPRESSION = "环境压制"
STATUS_TIMEFRAME_DOWNGRADE = "周期冲突降级"
STATUS_LOW_CONFIDENCE = "低置信观察"
STATUS_RISK_TRIGGER = "风险触发"
STATUS_CONFIRMED = "接近确认"
STATUS_WAITING = "等待确认"

OVERALL_RISK_PRIORITY = "风险优先"
OVERALL_GOOD = "条件较好"
OVERALL_SECOND_CONFIRM = "等待二次确认"
OVERALL_OBSERVE = "观察为主"

DEFENSIVE_VALIDATION_STATUSES = {
    STATUS_RISK_TRIGGER,
    STATUS_ENVIRONMENT_SUPPRESSION,
    STATUS_LOW_CONFIDENCE,
    STATUS_TIMEFRAME_DOWNGRADE,
}


@dataclass(frozen=True)
class ValidationStatusContext:
    condition_met: bool
    reverse: bool
    risk_multiplier: float
    factor_expected_level: str | None
    timeframe_conflict: str | None


@dataclass(frozen=True)
class ValidationStatusRule:
    name: str
    status: str
    matches: Callable[[ValidationStatusContext], bool]


@dataclass(frozen=True)
class ValidationOverallContext:
    confirmed_count: int
    has_risk_trigger: bool
    risk_multiplier: float
    timeframe_conflict: str | None


@dataclass(frozen=True)
class ValidationOverallRule:
    name: str
    status: str
    matches: Callable[[ValidationOverallContext], bool]


@dataclass(frozen=True)
class ValidationConfidenceRule:
    name: str
    adjustment: int
    matches: Callable[[TimeframeAlignmentReport | None], bool]


def build_signal_validation_report(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> SignalValidationReport:
    trend_factor = _find_factor(factor_lab, "trend_momentum")
    volume_factor = _find_factor(factor_lab, "volume_confirmation")
    risk_factor = _find_factor(factor_lab, "risk_pressure")
    chip_factor = _find_factor(factor_lab, "chip_position")
    items = [
        _trend_pullback_validation(feature, market_regime, trend_factor, timeframe),
        _breakout_validation(feature, market_regime, volume_factor, timeframe),
        _support_defense_validation(feature, market_regime, risk_factor, timeframe),
        _t_range_validation(feature, market_regime, chip_factor, timeframe),
    ]
    overall_status = _validation_overall_status(items, market_regime, timeframe)
    return SignalValidationReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        overall_status=overall_status,
        summary=_validation_summary(overall_status, items, market_regime, timeframe),
        items=items,
        notes=_validation_notes(timeframe),
    )


def _trend_pullback_validation(
    feature: FeatureSnapshot,
    market_regime: MarketRegimeReport,
    factor: StandardFactor | None,
    timeframe: TimeframeAlignmentReport | None,
) -> SignalValidationItem:
    condition_met = feature.trend_score >= 55 and feature.price >= feature.ma5
    return SignalValidationItem(
        name="趋势回踩验证",
        category="买点",
        status=_price_validation_status(
            condition_met,
            (feature.price, feature.ma5),
            market_regime,
            factor,
            timeframe,
        ),
        confidence=_validation_confidence(feature.signal_confidence, factor, market_regime, timeframe),
        trigger_condition=f"价格不低于5日线 {feature.ma5:.2f}，趋势评分至少 55 分。",
        confirmation_condition=(
            f"收盘不跌回5日线，同时量能不低于20日均量 {VOLUME_CONFIRMATION_RATIO:.1f} 倍；"
            f"当前量能 {feature.volume_ratio:.2f} 倍。"
        ),
        invalidation_condition=f"收盘跌破20日线 {feature.ma20:.2f} 或有效跌破支撑 {feature.support:.2f}。",
        historical_reference=_factor_reference(factor),
        action_hint="只作为回踩确认信号，不在下跌途中提前判定止跌。",
    )


def _breakout_validation(
    feature: FeatureSnapshot,
    market_regime: MarketRegimeReport,
    factor: StandardFactor | None,
    timeframe: TimeframeAlignmentReport | None,
) -> SignalValidationItem:
    condition_met = feature.price >= feature.resistance * 0.985 and feature.volume_ratio >= VOLUME_CONFIRMATION_RATIO
    return SignalValidationItem(
        name="压力突破验证",
        category="买点",
        status=_price_validation_status(
            condition_met,
            (feature.price, feature.resistance),
            market_regime,
            factor,
            timeframe,
        ),
        confidence=_validation_confidence(feature.signal_confidence, factor, market_regime, timeframe),
        trigger_condition=(
            f"价格接近或突破压力位 {feature.resistance:.2f}，"
            f"且放量不低于20日均量 {VOLUME_CONFIRMATION_RATIO:.1f} 倍。"
        ),
        confirmation_condition="突破后回踩不跌回压力位下方，量价热度评分（衍生）维持在60分附近或继续改善。",
        invalidation_condition="突破后快速缩量回落，或次日跌回压力位下方。",
        historical_reference=_factor_reference(factor),
        action_hint="适合右侧确认，不适合盘中一冲就追。",
    )


def _support_defense_validation(
    feature: FeatureSnapshot,
    market_regime: MarketRegimeReport,
    factor: StandardFactor | None,
    timeframe: TimeframeAlignmentReport | None,
) -> SignalValidationItem:
    condition_met = feature.price > feature.support * 1.01 and feature.price >= feature.ma20 * 0.985
    return SignalValidationItem(
        name="支撑防守验证",
        category="风控",
        status=_price_validation_status(
            condition_met,
            (feature.price, feature.support, feature.ma20),
            market_regime,
            factor,
            timeframe,
            reverse=True,
        ),
        confidence=_validation_confidence(feature.data_quality_score, factor, market_regime, timeframe),
        trigger_condition=(
            f"价格守在支撑 {feature.support:.2f} 上方 1% 以外，"
            f"且不明显跌破20日线 {feature.ma20:.2f}。"
        ),
        confirmation_condition="支撑附近缩量止跌，且次日能重新站回短期均线。",
        invalidation_condition=f"有效跌破支撑 {feature.support:.2f} 或20日线 {feature.ma20:.2f} 后不能快速修复。",
        historical_reference=_factor_reference(factor),
        action_hint="这是风险线，不是越跌越买的理由。",
    )


def _t_range_validation(
    feature: FeatureSnapshot,
    market_regime: MarketRegimeReport,
    factor: StandardFactor | None,
    timeframe: TimeframeAlignmentReport | None,
) -> SignalValidationItem:
    condition_met = feature.price > feature.support and feature.price < feature.resistance
    return SignalValidationItem(
        name="做T区间验证",
        category="做T",
        status=_price_validation_status(
            condition_met,
            (feature.price, feature.support, feature.resistance),
            market_regime,
            factor,
            timeframe,
        ),
        confidence=_validation_confidence(
            min(feature.signal_confidence, feature.data_quality_score),
            factor,
            market_regime,
            timeframe,
        ),
        trigger_condition=(
            f"价格严格高于支撑 {feature.support:.2f} 且低于压力 {feature.resistance:.2f}，"
            "不把边界触及当作区间内。"
        ),
        confirmation_condition=(
            "只用已有可卖底仓，低吸后必须能在区间上沿或分时转弱处高抛，"
            "不能把做T确认等同于新增仓位。"
        ),
        invalidation_condition="价格触及或跌破支撑、有效突破压力后脱离区间，成交突然放大向下，或盘口显示明显卖压。",
        historical_reference=_factor_reference(factor),
        action_hint="做T只服务于降低持仓成本，不等同于新增买入建议。",
    )


def _validation_notes(timeframe: TimeframeAlignmentReport | None) -> list[str]:
    notes = [
        "验证闭环把每条建议拆成触发、确认、失效和历史参考，避免单个信号直接变成买卖结论。",
        "状态为“等待确认”时，只说明条件接近，不代表已经满足。",
    ]
    if _timeframe_level_in(timeframe, CONSERVATIVE_TIMEFRAME_LEVELS):
        notes.append(
            f"多周期当前为「{timeframe.conflict_level}」，所有验证状态已按保守口径降级。"
        )
    return notes


def _validation_status(
    condition_met: bool,
    market_regime: MarketRegimeReport,
    factor: StandardFactor | None,
    timeframe: TimeframeAlignmentReport | None = None,
    *,
    reverse: bool = False,
) -> str:
    context = _validation_status_context(condition_met, market_regime, factor, timeframe, reverse=reverse)
    for rule in VALIDATION_STATUS_RULES:
        if rule.matches(context):
            return rule.status
    return STATUS_WAITING


def _price_validation_status(
    condition_met: bool,
    required_prices: tuple[object, ...],
    market_regime: MarketRegimeReport,
    factor: StandardFactor | None,
    timeframe: TimeframeAlignmentReport | None = None,
    *,
    reverse: bool = False,
) -> str:
    if not _required_prices_are_valid(required_prices):
        return STATUS_WAITING
    return _validation_status(condition_met, market_regime, factor, timeframe, reverse=reverse)


def _required_prices_are_valid(values: tuple[object, ...]) -> bool:
    for value in values:
        parsed = _finite_float(value)
        if parsed is None or parsed <= 0:
            return False
    return True


def _validation_status_context(
    condition_met: bool,
    market_regime: MarketRegimeReport,
    factor: StandardFactor | None,
    timeframe: TimeframeAlignmentReport | None = None,
    *,
    reverse: bool = False,
) -> ValidationStatusContext:
    expected_level = factor.calibration.expected_level if factor and factor.calibration else None
    return ValidationStatusContext(
        condition_met=condition_met,
        reverse=reverse,
        risk_multiplier=_number_or_default(market_regime.risk_multiplier, DEFAULT_RISK_MULTIPLIER),
        factor_expected_level=expected_level,
        timeframe_conflict=timeframe.conflict_level if timeframe else None,
    )


def _number_or_default(value: object, default: float) -> float:
    parsed = _finite_float(value)
    return parsed if parsed is not None else default


def _timeframe_level_in(timeframe: TimeframeAlignmentReport | None, levels: set[str]) -> bool:
    return bool(timeframe and timeframe.conflict_level in levels)


def _timeframe_level_is(timeframe: TimeframeAlignmentReport | None, level: str) -> bool:
    return bool(timeframe and timeframe.conflict_level == level)


def _timeframe_blocks_confidence(timeframe: TimeframeAlignmentReport | None) -> bool:
    return _timeframe_level_in(timeframe, BLOCKING_TIMEFRAME_LEVELS)


def _timeframe_is_medium_conflict(timeframe: TimeframeAlignmentReport | None) -> bool:
    return _timeframe_level_is(timeframe, TIMEFRAME_MEDIUM_CONFLICT)


def _environment_suppresses_status(context: ValidationStatusContext) -> bool:
    return context.risk_multiplier >= ENVIRONMENT_RISK_MULTIPLIER_THRESHOLD


def _timeframe_blocks_status(context: ValidationStatusContext) -> bool:
    return context.timeframe_conflict in BLOCKING_TIMEFRAME_LEVELS


def _factor_is_low_confidence(context: ValidationStatusContext) -> bool:
    return context.factor_expected_level in WEAK_FACTOR_EXPECTED_LEVELS


def _reverse_condition_triggers_risk(context: ValidationStatusContext) -> bool:
    return context.reverse and not context.condition_met


def _mixed_timeframe_lowers_confirmed_status(context: ValidationStatusContext) -> bool:
    return context.condition_met and context.timeframe_conflict in MIXED_TIMEFRAME_LEVELS


def _condition_is_confirmed(context: ValidationStatusContext) -> bool:
    return context.condition_met


VALIDATION_STATUS_RULES = (
    ValidationStatusRule("environment_suppression", STATUS_ENVIRONMENT_SUPPRESSION, _environment_suppresses_status),
    ValidationStatusRule("reverse_risk", STATUS_RISK_TRIGGER, _reverse_condition_triggers_risk),
    ValidationStatusRule("timeframe_block", STATUS_TIMEFRAME_DOWNGRADE, _timeframe_blocks_status),
    ValidationStatusRule("weak_factor", STATUS_LOW_CONFIDENCE, _factor_is_low_confidence),
    ValidationStatusRule(
        "mixed_timeframe_confirmed",
        STATUS_LOW_CONFIDENCE,
        _mixed_timeframe_lowers_confirmed_status,
    ),
    ValidationStatusRule("confirmed", STATUS_CONFIRMED, _condition_is_confirmed),
)


def _validation_confidence(
    base: int,
    factor: StandardFactor | None,
    market_regime: MarketRegimeReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> int:
    score = _validation_base_confidence(base, factor)
    score += _number_or_default(market_regime.confidence_adjustment, DEFAULT_CONFIDENCE_ADJUSTMENT)
    score += _timeframe_confidence_adjustment(timeframe)
    return _clamp(score)


def _validation_base_confidence(base: int, factor: StandardFactor | None) -> int:
    base_score = _number_or_default(base, 0)
    if not factor:
        return round(base_score)
    factor_score = _number_or_default(factor.score, 0)
    stability_score = (
        _number_or_default(factor.calibration.stability_score, DEFAULT_FACTOR_STABILITY_SCORE)
        if factor.calibration
        else DEFAULT_FACTOR_STABILITY_SCORE
    )
    return round(base_score * 0.45 + factor_score * 0.25 + stability_score * 0.3)


def _timeframe_confidence_adjustment(timeframe: TimeframeAlignmentReport | None) -> int:
    for rule in VALIDATION_CONFIDENCE_TIMEFRAME_RULES:
        if rule.matches(timeframe):
            return rule.adjustment
    return 0


VALIDATION_CONFIDENCE_TIMEFRAME_RULES = (
    ValidationConfidenceRule("blocking_timeframe", -12, _timeframe_blocks_confidence),
    ValidationConfidenceRule("mixed_timeframe", -6, _timeframe_is_medium_conflict),
)


def _validation_overall_status(
    items: list[SignalValidationItem],
    market_regime: MarketRegimeReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> str:
    context = _validation_overall_context(items, market_regime, timeframe)
    for rule in VALIDATION_OVERALL_RULES:
        if rule.matches(context):
            return rule.status
    return OVERALL_OBSERVE


def _validation_overall_context(
    items: list[SignalValidationItem],
    market_regime: MarketRegimeReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> ValidationOverallContext:
    return ValidationOverallContext(
        confirmed_count=sum(1 for item in items if item.status == STATUS_CONFIRMED),
        has_risk_trigger=any(item.status == STATUS_RISK_TRIGGER for item in items),
        risk_multiplier=_number_or_default(market_regime.risk_multiplier, DEFAULT_RISK_MULTIPLIER),
        timeframe_conflict=timeframe.conflict_level if timeframe else None,
    )


def _timeframe_blocks_overall(context: ValidationOverallContext) -> bool:
    return context.timeframe_conflict in BLOCKING_TIMEFRAME_LEVELS


def _risk_priority_overall(context: ValidationOverallContext) -> bool:
    return context.risk_multiplier >= ENVIRONMENT_RISK_MULTIPLIER_THRESHOLD or context.has_risk_trigger


def _mixed_timeframe_requires_second_confirm(context: ValidationOverallContext) -> bool:
    return context.timeframe_conflict == TIMEFRAME_MEDIUM_CONFLICT and context.confirmed_count > 0


def _multiple_confirmations_are_good(context: ValidationOverallContext) -> bool:
    return context.confirmed_count >= 2 and context.risk_multiplier <= LOW_RISK_MULTIPLIER_THRESHOLD


def _single_confirmation_needs_followup(context: ValidationOverallContext) -> bool:
    return context.confirmed_count >= 1


VALIDATION_OVERALL_RULES = (
    ValidationOverallRule("timeframe_block", OVERALL_RISK_PRIORITY, _timeframe_blocks_overall),
    ValidationOverallRule("risk_priority", OVERALL_RISK_PRIORITY, _risk_priority_overall),
    ValidationOverallRule(
        "mixed_timeframe_second_confirm",
        OVERALL_SECOND_CONFIRM,
        _mixed_timeframe_requires_second_confirm,
    ),
    ValidationOverallRule("multiple_confirmations", OVERALL_GOOD, _multiple_confirmations_are_good),
    ValidationOverallRule("single_confirmation", OVERALL_SECOND_CONFIRM, _single_confirmation_needs_followup),
)


def _validation_summary(
    overall_status: str,
    items: list[SignalValidationItem],
    market_regime: MarketRegimeReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> str:
    confirmed = [item.name for item in items if item.status == STATUS_CONFIRMED]
    risk = [item.name for item in items if item.status in DEFENSIVE_VALIDATION_STATUSES]
    confirmed_text = "、".join(confirmed) if confirmed else "暂无接近确认的信号"
    risk_text = "、".join(risk) if risk else "暂无高优先级风险验证项"
    timeframe_text = f"；多周期为「{timeframe.conflict_level}」" if timeframe else ""
    risk_multiplier = _number_or_default(market_regime.risk_multiplier, DEFAULT_RISK_MULTIPLIER)
    return (
        f"{overall_status}：接近确认的是{confirmed_text}；需要防守的是{risk_text}；"
        f"环境风险倍率 {risk_multiplier:.2f}{timeframe_text}。"
    )
