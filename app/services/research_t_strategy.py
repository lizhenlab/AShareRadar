from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import AnalysisResult, FeatureSnapshot, MarketRegimeReport, SignalValidationReport, TStrategyAssistantReport
from app.utils.market_data import finite_float


@dataclass(frozen=True)
class TStrategyContext:
    analysis: AnalysisResult | None
    feature: FeatureSnapshot
    market_regime: MarketRegimeReport
    validation: SignalValidationReport | None
    price: float
    support: float
    resistance: float
    atr14: float
    atr_pct: float
    ma5: float
    trend_score: int
    risk_multiplier: float
    width_pct: float
    swing_buffer: float


@dataclass(frozen=True)
class TStrategyRule:
    name: str
    label: str
    matches: Callable[[TStrategyContext], bool]


def build_t_strategy_assistant_report(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
) -> TStrategyAssistantReport:
    context = _t_strategy_context(analysis, feature, market_regime, validation)
    style = _t_strategy_style_from_context(context)
    suitability = _t_strategy_suitability(context)
    low_zone = _low_zone(context)
    high_zone = _high_zone(context)
    return TStrategyAssistantReport(
        style=style,
        suitability=suitability,
        summary=f"{style}，{suitability}。做T只服务于降低已有底仓成本，不等同于新增买入。",
        low_zone=low_zone,
        high_zone=high_zone,
        execution_steps=[
            "先确认手里有可卖底仓，今日新增买入部分不参与当日T。",
            f"低吸只看 {low_zone} 缩量止跌，不在放量下跌中接。",
            f"高抛只看 {high_zone} 冲高乏力或接近压力，不恋战。",
        ],
        stop_conditions=[
            _support_stop_condition(context),
            "成交突然放大向下或盘口卖压增强。",
            "区间宽度不足以覆盖交易成本和滑点。",
        ],
    )


def _t_strategy_style(feature: FeatureSnapshot, market_regime: MarketRegimeReport) -> str:
    context = _t_strategy_context(None, feature, market_regime, None)
    return _t_strategy_style_from_context(context)


def _t_strategy_context(
    analysis: AnalysisResult | None,
    feature: FeatureSnapshot,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport | None,
) -> TStrategyContext:
    price = _positive_or_zero(feature.price)
    support = _positive_or_zero(feature.support)
    resistance = _positive_or_zero(feature.resistance)
    atr14 = _non_negative_or_zero(feature.atr14)
    atr_pct = _non_negative_or_zero(feature.atr_pct)
    ma5 = _positive_or_zero(feature.ma5)
    return TStrategyContext(
        analysis=analysis,
        feature=feature,
        market_regime=market_regime,
        validation=validation,
        price=price,
        support=support,
        resistance=resistance,
        atr14=atr14,
        atr_pct=atr_pct,
        ma5=ma5,
        trend_score=_score_or_zero(feature.trend_score),
        risk_multiplier=_positive_or_one(market_regime.risk_multiplier),
        width_pct=_range_width_pct(price, support, resistance),
        swing_buffer=max(atr14, price * 0.012),
    )


def _range_width_pct(price: float, support: float, resistance: float) -> float:
    if price <= 0 or resistance <= support:
        return 0
    return (resistance - support) / price * 100


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
    parsed = finite_float(value)
    if parsed is None:
        return 0
    return round(min(max(parsed, 0), 100))


def _t_strategy_style_from_context(context: TStrategyContext) -> str:
    for rule in T_STRATEGY_STYLE_RULES:
        if rule.matches(context):
            return rule.label
    return "区间震荡型"


def _t_strategy_suitability(context: TStrategyContext) -> str:
    for rule in T_STRATEGY_SUITABILITY_RULES:
        if rule.matches(context):
            return rule.label
    return "等待更大区间"


def _low_zone(context: TStrategyContext) -> str:
    if context.price > 0 and context.support > 0:
        return _zone_text(max(context.support, context.price - context.swing_buffer))
    if context.price > 0:
        return _zone_text(max(0, context.price - context.swing_buffer))
    if context.support > 0:
        return _zone_text(context.support)
    return "待确认"


def _high_zone(context: TStrategyContext) -> str:
    if context.price > 0 and context.resistance > 0:
        return _zone_text(min(context.resistance, context.price + context.swing_buffer))
    if context.price > 0:
        return _zone_text(context.price + context.swing_buffer)
    if context.resistance > 0:
        return _zone_text(context.resistance)
    return "待确认"


def _zone_text(value: float) -> str:
    return f"{value:.2f} 附近" if value > 0 else "待确认"


def _support_stop_condition(context: TStrategyContext) -> str:
    if context.support > 0:
        return f"有效跌破支撑 {context.support:.2f}。"
    return "支撑位待确认，若放量下跌或区间边界失效则停止做T。"


def _active_t_strategy_blocked(context: TStrategyContext) -> bool:
    return bool(context.analysis and context.analysis.data_quality.score < 70) or context.risk_multiplier >= 1.28


def _range_is_tradable(context: TStrategyContext) -> bool:
    return context.width_pct >= max(1.2, context.atr_pct * 0.8)


def _validation_allows_t_strategy(context: TStrategyContext) -> bool:
    return bool(context.validation and context.validation.overall_status != "风险优先")


def _narrow_t_range(context: TStrategyContext) -> bool:
    return context.width_pct < max(1.2, context.atr_pct * 0.7)


def _trend_rollable(context: TStrategyContext) -> bool:
    return context.trend_score >= 65 and context.price > 0 and context.price >= context.ma5


T_STRATEGY_STYLE_RULES = (
    TStrategyRule("risk_defensive", "风险防守型", lambda context: context.risk_multiplier >= 1.25),
    TStrategyRule("narrow_waiting", "窄幅等待型", _narrow_t_range),
    TStrategyRule("trend_rolling", "趋势滚动型", _trend_rollable),
)


T_STRATEGY_SUITABILITY_RULES = (
    TStrategyRule("active_t_blocked", "不适合主动做T", _active_t_strategy_blocked),
    TStrategyRule("tradable_range", "仅底仓可做T", lambda context: _range_is_tradable(context) and _validation_allows_t_strategy(context)),
)
