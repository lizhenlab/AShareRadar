from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from app.models.schemas import (
    AnalysisResult,
    FactorLabReport,
    FeatureSnapshot,
    MarketRegimeReport,
    StockInsightBundle,
)
from app.services.research_breadth import MarketBreadthSnapshot, build_market_breadth_snapshot
from app.services.scoring import bounded_int as _bounded_int
from app.utils.market_data import finite_float as _finite_float


LOW_CONFIDENCE_DATA_SCORE = 50
DEGRADED_DATA_SCORE = 70
COLD_BREADTH_SCORE = 35
WARM_BREADTH_SCORE = 65
RISK_MULTIPLIER_MIN = 0.72
RISK_MULTIPLIER_MAX = 1.48
DEFAULT_SCORE = 50
DEFAULT_DATA_QUALITY_SCORE = 0
SUGGESTION_LIMIT = 5
EVIDENCE_LIMIT = 6
BREADTH_COLD_SUGGESTION_SCORE = 40
ANALYSIS_RISK_ADJUSTMENTS = {"高风险": 0.22, "中等风险": 0.1}
STOCK_TAILWIND_INDUSTRY_LABELS = frozenset(
    {
        "行业顺风",
        "行业小幅配合",
        "行业待确认",
    }
)

StockStateSuggestion = Callable[["RegimeContext"], str]


@dataclass(frozen=True)
class MarketRegimeRule:
    label: str
    matches: Callable[["RegimeContext", str], bool]


@dataclass(frozen=True)
class FactorLabRiskRule:
    matches: Callable[["RegimeContext", FactorLabReport, float], bool]
    adjustment: Callable[["RegimeContext", FactorLabReport, float], float]


@dataclass(frozen=True)
class RegimeRiskAdjustment:
    name: str
    value: float


@dataclass(frozen=True)
class RegimeMetrics:
    data_quality_score: int
    trend_score: int
    fund_flow_score: int
    factor_score: int
    price: float
    support: float
    resistance: float
    ma5: float
    industry_change_pct: float | None


@dataclass(frozen=True)
class RegimeClassification:
    stock_state: str
    market_label: str


@dataclass(frozen=True)
class RegimeRiskProfile:
    multiplier: float
    confidence_adjustment: int


@dataclass(frozen=True)
class RegimeContext:
    analysis: AnalysisResult
    insights: StockInsightBundle
    feature: FeatureSnapshot
    factor_lab: FactorLabReport | None
    breadth: MarketBreadthSnapshot
    industry_label: str
    metrics: RegimeMetrics

    @property
    def low_confidence_data(self) -> bool:
        return self.metrics.data_quality_score < LOW_CONFIDENCE_DATA_SCORE

    @property
    def degraded_data(self) -> bool:
        return self.metrics.data_quality_score < DEGRADED_DATA_SCORE

    @property
    def hard_risk(self) -> bool:
        return self.analysis.risk_level == "高风险" or self.insights.abnormal_events.level == "风险"

    @property
    def right_side_candidate(self) -> bool:
        return (
            self.metrics.trend_score >= 65
            and self.metrics.fund_flow_score >= 58
            and self.metrics.factor_score >= 60
        )

    @property
    def near_support(self) -> bool:
        return (
            self.metrics.price > 0
            and self.metrics.support > 0
            and self.metrics.price <= self.metrics.support * 1.03
        )

    @property
    def near_resistance(self) -> bool:
        return (
            self.metrics.price > 0
            and self.metrics.resistance > 0
            and self.metrics.price >= self.metrics.resistance * 0.985
        )

    @property
    def cold_breadth(self) -> bool:
        return self.breadth.score <= COLD_BREADTH_SCORE

    @property
    def warm_breadth(self) -> bool:
        return self.breadth.score >= WARM_BREADTH_SCORE


def build_market_regime_report(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport | None = None,
    breadth: MarketBreadthSnapshot | None = None,
) -> MarketRegimeReport:
    context = _build_regime_context(analysis, insights, feature, factor_lab, breadth)
    classification = _regime_classification(context)
    risk_profile = _regime_risk_profile(context)
    return MarketRegimeReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        market_label=classification.market_label,
        breadth_label=context.breadth.label,
        breadth_score=context.breadth.score,
        industry_label=context.industry_label,
        stock_state=classification.stock_state,
        risk_multiplier=risk_profile.multiplier,
        confidence_adjustment=risk_profile.confidence_adjustment,
        suggestions=_regime_suggestions(context, classification.stock_state),
        evidence=_regime_evidence(context),
    )


def _build_regime_context(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport | None,
    breadth: MarketBreadthSnapshot | None,
) -> RegimeContext:
    metrics = _regime_metrics(feature, factor_lab)
    return RegimeContext(
        analysis=analysis,
        insights=insights,
        feature=feature,
        factor_lab=factor_lab,
        breadth=_clean_breadth_snapshot(breadth),
        industry_label=_industry_regime_label(feature, metrics.industry_change_pct),
        metrics=metrics,
    )


def _regime_metrics(feature: FeatureSnapshot, factor_lab: FactorLabReport | None) -> RegimeMetrics:
    return RegimeMetrics(
        data_quality_score=_data_quality_score(feature.data_quality_score),
        trend_score=_score(feature.trend_score),
        fund_flow_score=_score(feature.fund_flow_score),
        factor_score=_factor_score(feature, factor_lab),
        price=_positive_number(feature.price),
        support=_positive_number(feature.support),
        resistance=_positive_number(feature.resistance),
        ma5=_positive_number(feature.ma5),
        industry_change_pct=_optional_number(feature.industry_change_pct),
    )


def _factor_score(feature: FeatureSnapshot, factor_lab: FactorLabReport | None) -> int:
    if factor_lab:
        return _score(factor_lab.total_score)
    return _score(feature.trend_score)


def _clean_breadth_snapshot(breadth: MarketBreadthSnapshot | None) -> MarketBreadthSnapshot:
    snapshot = breadth or build_market_breadth_snapshot([])
    return replace(
        snapshot,
        score=_score(snapshot.score),
        avg_change_pct=_number_or_default(snapshot.avg_change_pct),
        risk_adjustment=_number_or_default(snapshot.risk_adjustment),
    )


def _industry_regime_label(feature: FeatureSnapshot, industry_change_pct: float | None = None) -> str:
    change_pct = _optional_number(feature.industry_change_pct) if industry_change_pct is None else industry_change_pct
    if not _non_empty_text(feature.industry_name) or change_pct is None:
        return "行业待确认"
    if change_pct >= 1.2:
        return "行业顺风"
    if change_pct <= -1.2:
        return "行业逆风"
    if change_pct > 0:
        return "行业小幅配合"
    return "行业震荡"


def _regime_classification(context: RegimeContext) -> RegimeClassification:
    stock_state = _stock_state_label(context)
    return RegimeClassification(
        stock_state=stock_state,
        market_label=_market_regime_label(context, stock_state),
    )


def _stock_state_label(context: RegimeContext) -> str:
    if context.low_confidence_data:
        return "数据不足"
    if context.hard_risk:
        return "风险优先"
    if context.right_side_candidate:
        return "右侧偏强"
    if context.near_support:
        return "支撑观察"
    if context.near_resistance:
        return "压力确认"
    return "震荡等待"


def _market_regime_label(context: RegimeContext, stock_state: str) -> str:
    return next(rule.label for rule in MARKET_REGIME_RULES if rule.matches(context, stock_state))


def _is_low_confidence_regime(context: RegimeContext, _: str) -> bool:
    return context.low_confidence_data


def _is_hard_risk_regime(context: RegimeContext, stock_state: str) -> bool:
    return stock_state == "风险优先" or context.analysis.risk_level == "高风险"


def _is_cold_breadth_regime(context: RegimeContext, _: str) -> bool:
    return context.cold_breadth


def _is_stock_tailwind_regime(context: RegimeContext, _: str) -> bool:
    return context.metrics.factor_score >= 65 and context.industry_label in STOCK_TAILWIND_INDUSTRY_LABELS


def _is_warm_breadth_regime(context: RegimeContext, _: str) -> bool:
    return context.warm_breadth


def _is_industry_headwind_regime(context: RegimeContext, _: str) -> bool:
    return "逆风" in context.industry_label


def _is_default_regime(_: RegimeContext, __: str) -> bool:
    return True


MARKET_REGIME_RULES = (
    MarketRegimeRule("低置信环境", _is_low_confidence_regime),
    MarketRegimeRule("风险环境", _is_hard_risk_regime),
    MarketRegimeRule("市场偏冷环境", _is_cold_breadth_regime),
    MarketRegimeRule("个股顺风环境", _is_stock_tailwind_regime),
    MarketRegimeRule("市场偏暖环境", _is_warm_breadth_regime),
    MarketRegimeRule("行业逆风环境", _is_industry_headwind_regime),
    MarketRegimeRule("中性观察环境", _is_default_regime),
)


def _regime_risk_multiplier(context: RegimeContext) -> float:
    multiplier = 1.0 + _risk_adjustment_total(_regime_risk_adjustments(context))
    return round(
        max(RISK_MULTIPLIER_MIN, min(RISK_MULTIPLIER_MAX, _number_or_default(multiplier, 1.0))),
        2,
    )


def _regime_risk_profile(context: RegimeContext) -> RegimeRiskProfile:
    multiplier = _regime_risk_multiplier(context)
    return RegimeRiskProfile(
        multiplier=multiplier,
        confidence_adjustment=_confidence_adjustment(multiplier),
    )


def _confidence_adjustment(risk_multiplier: float) -> int:
    return _bounded_int((1 - risk_multiplier) * 45, -20, 12, round_value=True)


def _regime_risk_adjustments(context: RegimeContext) -> tuple[RegimeRiskAdjustment, ...]:
    return (
        RegimeRiskAdjustment("data_quality", _data_quality_risk_adjustment(context.metrics.data_quality_score)),
        RegimeRiskAdjustment("analysis_risk", ANALYSIS_RISK_ADJUSTMENTS.get(context.analysis.risk_level, 0)),
        RegimeRiskAdjustment("abnormal_event", 0.12 if context.insights.abnormal_events.level == "风险" else 0),
        RegimeRiskAdjustment("industry", _industry_risk_adjustment(context.industry_label)),
        RegimeRiskAdjustment("factor_lab", _factor_lab_risk_adjustment(context)),
        RegimeRiskAdjustment("market_breadth", context.breadth.risk_adjustment),
    )


def _risk_adjustment_total(adjustments: tuple[RegimeRiskAdjustment, ...]) -> float:
    values = (_finite_float(item.value) for item in adjustments)
    return sum(value for value in values if value is not None)


def _data_quality_risk_adjustment(score: int) -> float:
    if score < LOW_CONFIDENCE_DATA_SCORE:
        return 0.28
    if score < DEGRADED_DATA_SCORE:
        return 0.14
    return 0


def _industry_risk_adjustment(industry_label: str) -> float:
    if "逆风" in industry_label:
        return 0.1
    if "顺风" in industry_label:
        return -0.06
    return 0


def _factor_lab_risk_adjustment(context: RegimeContext) -> float:
    factor_lab = context.factor_lab
    if not factor_lab:
        return 0
    positive_scale = _positive_factor_adjustment_scale(context)
    return sum(
        rule.adjustment(context, factor_lab, positive_scale)
        for rule in FACTOR_LAB_RISK_RULES
        if rule.matches(context, factor_lab, positive_scale)
    )


def _has_positive_factor_edge_with_sample(
    _: RegimeContext,
    factor_lab: FactorLabReport,
    positive_scale: float,
) -> bool:
    return (
        positive_scale > 0
        and _non_negative_int(factor_lab.calibration_sample_count) >= 24
        and _positive_factor_edge(factor_lab)
    )


def _positive_factor_edge_sample_adjustment(
    _: RegimeContext,
    __: FactorLabReport,
    positive_scale: float,
) -> float:
    return -0.06 * positive_scale


def _has_confident_factor_score(
    _: RegimeContext,
    factor_lab: FactorLabReport,
    positive_scale: float,
) -> bool:
    return (
        positive_scale > 0
        and _score(factor_lab.total_score) >= 66
        and _score(factor_lab.calibrated_confidence, default=0) >= 58
    )


def _confident_factor_score_adjustment(
    _: RegimeContext,
    __: FactorLabReport,
    positive_scale: float,
) -> float:
    return -0.08 * positive_scale


def _has_low_factor_score(_: RegimeContext, factor_lab: FactorLabReport, __: float) -> bool:
    return _score(factor_lab.total_score) <= 45


def _low_factor_score_adjustment(_: RegimeContext, __: FactorLabReport, ___: float) -> float:
    return 0.12


def _has_negative_factor_edge(_: RegimeContext, factor_lab: FactorLabReport, __: float) -> bool:
    return _negative_factor_edge(factor_lab)


def _negative_factor_edge_adjustment(_: RegimeContext, __: FactorLabReport, ___: float) -> float:
    return 0.05


FACTOR_LAB_RISK_RULES = (
    FactorLabRiskRule(_has_positive_factor_edge_with_sample, _positive_factor_edge_sample_adjustment),
    FactorLabRiskRule(_has_confident_factor_score, _confident_factor_score_adjustment),
    FactorLabRiskRule(_has_low_factor_score, _low_factor_score_adjustment),
    FactorLabRiskRule(_has_negative_factor_edge, _negative_factor_edge_adjustment),
)


def _positive_factor_adjustment_scale(context: RegimeContext) -> float:
    if context.low_confidence_data:
        return 0
    if context.degraded_data:
        return 0.5
    return 1


def _positive_factor_edge(factor_lab: FactorLabReport) -> bool:
    return (
        _non_negative_int(factor_lab.positive_factor_count)
        >= _non_negative_int(factor_lab.negative_factor_count) + 2
    )


def _negative_factor_edge(factor_lab: FactorLabReport) -> bool:
    return (
        _non_negative_int(factor_lab.negative_factor_count)
        >= _non_negative_int(factor_lab.positive_factor_count) + 2
    )


def _regime_suggestions(context: RegimeContext, stock_state: str) -> list[str]:
    suggestions: list[str] = []
    if context.degraded_data:
        suggestions.append("先恢复数据质量和多源一致性，再放大任何买卖点权重。")
    suggestions.append(_stock_state_suggestion(stock_state, context))
    suggestions.extend(
        item
        for item in (
            _industry_suggestion(context.industry_label),
            _breadth_suggestion(context.breadth),
            _top_negative_suggestion(context.factor_lab),
            _low_factor_sample_suggestion(context.factor_lab),
            _positive_factor_suggestion(context.factor_lab),
        )
        if item
    )
    if not suggestions:
        action = context.analysis.action_advice.action
        suggestions.append(f"当前建议仍以「{action}」为主，按条件清单执行。")
    return suggestions[:SUGGESTION_LIMIT]


def _stock_state_suggestion(stock_state: str, context: RegimeContext) -> str:
    builder = STOCK_STATE_SUGGESTIONS.get(stock_state, _default_stock_state_suggestion)
    return builder(context)


def _right_side_suggestion(context: RegimeContext) -> str:
    return (
        f"只在回踩不破5日线 {_price_text(context.metrics.ma5)} "
        f"或放量站稳压力位 {_price_text(context.metrics.resistance)} 时提高积极度。"
    )


def _support_suggestion(context: RegimeContext) -> str:
    return (
        f"靠近支撑 {_price_text(context.metrics.support)} 时先看缩量止跌，"
        "不把下跌过程当成确定买点。"
    )


def _resistance_suggestion(context: RegimeContext) -> str:
    return f"压力位 {_price_text(context.metrics.resistance)} 附近优先看放量站稳，冲高回落则降级。"


def _risk_first_suggestion(_: RegimeContext) -> str:
    return "先处理硬风险，等放量下跌、异动风险或20日线破位修复后再评估。"


def _default_stock_state_suggestion(_: RegimeContext) -> str:
    return "按支撑、压力和量能三件事等待确认，避免单日涨跌驱动判断。"


STOCK_STATE_SUGGESTIONS: dict[str, StockStateSuggestion] = {
    "右侧偏强": _right_side_suggestion,
    "支撑观察": _support_suggestion,
    "压力确认": _resistance_suggestion,
    "风险优先": _risk_first_suggestion,
}


def _industry_suggestion(industry_label: str) -> str | None:
    if "逆风" in industry_label:
        return "行业逆风时，个股信号需要更强的量价确认才能上调评级。"
    return None


def _breadth_suggestion(breadth: MarketBreadthSnapshot) -> str | None:
    if breadth.score <= BREADTH_COLD_SUGGESTION_SCORE:
        return "市场宽度偏冷时，优先看防守线，不把个别异动当成普遍回暖。"
    if breadth.score >= WARM_BREADTH_SCORE:
        return "市场宽度偏暖时，可优先跟踪放量站稳的右侧确认机会。"
    return None


def _top_negative_suggestion(factor_lab: FactorLabReport | None) -> str | None:
    factor_name = _first_non_empty_text(getattr(factor_lab, "top_negative", [])) if factor_lab else None
    if factor_name:
        return f"优先跟踪拖累因子「{factor_name}」是否修复。"
    return None


def _low_factor_sample_suggestion(factor_lab: FactorLabReport | None) -> str | None:
    if factor_lab and _non_negative_int(factor_lab.calibration_sample_count) < 8:
        return "因子历史样本仍偏少，建议把实验室分数当作低置信辅助项。"
    return None


def _positive_factor_suggestion(factor_lab: FactorLabReport | None) -> str | None:
    if factor_lab and _positive_factor_edge(factor_lab):
        return "当前正向因子略占优，可以优先等价量和环境一起确认，而不是单看价格。"
    return None


def _regime_evidence(context: RegimeContext) -> list[str]:
    feature = context.feature
    evidence = [
        (
            f"数据质量 {feature.data_quality_level} "
            f"{_score_text(feature.data_quality_score, default=DEFAULT_DATA_QUALITY_SCORE)}。"
        ),
        (
            f"个股趋势 {feature.trend_label} {_score_text(feature.trend_score)}，"
            f"资金 {_score_text(feature.fund_flow_score)}。"
        ),
        _breadth_summary_text(context.breadth),
        _factor_lab_evidence(context.factor_lab),
    ]
    industry_name = _non_empty_text(feature.industry_name)
    if industry_name and context.metrics.industry_change_pct is not None:
        evidence.append(f"行业 {industry_name} 涨跌幅 {context.metrics.industry_change_pct:.2f}%。")
    if context.insights.abnormal_events.events:
        evidence.append(
            f"异动：{context.insights.abnormal_events.main_signal} / {context.insights.abnormal_events.level}。"
        )
    return evidence[:EVIDENCE_LIMIT]


def _factor_lab_evidence(factor_lab: FactorLabReport | None) -> str:
    if not factor_lab:
        return "因子实验室暂未参与环境判断。"
    return (
        f"因子总分 {_score_text(factor_lab.total_score, with_unit=False)}，"
        f"校准置信度 {_percent_text(factor_lab.calibrated_confidence)}。"
    )


def _score(value: object, *, default: int = DEFAULT_SCORE) -> int:
    return _bounded_int(value, 0, 100, default=default, round_value=True)


def _data_quality_score(value: object) -> int:
    return _score(value, default=DEFAULT_DATA_QUALITY_SCORE)


def _non_negative_int(value: object) -> int:
    return _bounded_int(value, 0, 1_000_000, default=0, round_value=True)


def _number_or_default(value: object, default: float = 0.0) -> float:
    parsed = _finite_float(value)
    return parsed if parsed is not None else default


def _positive_number(value: object) -> float:
    parsed = _finite_float(value)
    return parsed if parsed is not None and parsed > 0 else 0.0


def _optional_number(value: object) -> float | None:
    return _finite_float(value)


def _non_empty_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_non_empty_text(values: object) -> str | None:
    try:
        candidates = list(values) if values is not None else []
    except TypeError:
        candidates = [values]
    for item in candidates:
        if text := _non_empty_text(item):
            return text
    return None


def _breadth_summary_text(breadth: MarketBreadthSnapshot) -> str:
    return _non_empty_text(getattr(breadth, "summary", None)) or "市场宽度样本不足，环境参考待确认。"


def _price_text(value: float) -> str:
    return f"{value:.2f}" if value > 0 else "待确认"


def _score_text(value: object, *, default: int = DEFAULT_SCORE, with_unit: bool = True) -> str:
    if _finite_float(value) is None:
        return "待确认"
    text = str(_score(value, default=default))
    return f"{text} 分" if with_unit else text


def _percent_text(value: object) -> str:
    if _finite_float(value) is None:
        return "待确认"
    return f"{_score(value, default=0)}%"
