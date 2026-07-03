from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import AnalysisResult, FactorScore, FundFlowAnalysis, KeyPriceLevel, OrderPressure, StockEventSummary, StockOverview
from app.services.scoring import clamp_score, score_level
from app.utils.market_data import finite_float

FUNDAMENTAL_BASE_SCORE = 55
LOW_PE_THRESHOLD = 25
HIGH_PE_THRESHOLD = 60
LOW_PB_THRESHOLD = 3
HIGH_PB_THRESHOLD = 8
DATA_QUALITY_CAP_THRESHOLD = 70
WEAK_DATA_QUALITY_THRESHOLD = 50
LOW_SIGNAL_CONFIDENCE_THRESHOLD = 60
WEAK_TREND_THRESHOLD = 45
STRONG_TREND_THRESHOLD = 65
STRONG_FUND_FLOW_THRESHOLD = 60
WEAK_FUND_FLOW_THRESHOLD = 50


@dataclass(frozen=True)
class ValuationMetricSpec:
    label: str
    low_threshold: float
    high_threshold: float
    adjustment: int
    missing_data: str


@dataclass(frozen=True)
class FundamentalFieldResult:
    score_adjustment: int = 0
    evidence: str | None = None
    missing_data: str | None = None


@dataclass(frozen=True)
class MainConflictContext:
    analysis: AnalysisResult
    fund_flow: FundFlowAnalysis
    order_pressure: OrderPressure


@dataclass(frozen=True)
class MainConflictRule:
    name: str
    message: str
    matches: Callable[[MainConflictContext], bool]


@dataclass(frozen=True)
class OverviewScores:
    factors: list[FactorScore]
    factor_score: int
    signal_quality_score: int
    total_score: int


VALUATION_METRIC_SPECS = {
    "pe": ValuationMetricSpec("PE", LOW_PE_THRESHOLD, HIGH_PE_THRESHOLD, 8, "PE"),
    "pb": ValuationMetricSpec("PB", LOW_PB_THRESHOLD, HIGH_PB_THRESHOLD, 6, "PB"),
}


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = " ".join(value.split())
    else:
        try:
            float(value)
        except (TypeError, ValueError):
            text = " ".join(str(value).split())
        else:
            return None
    invalid_text = {"nan", "none", "null", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}
    return text if text and text.lower() not in invalid_text else None


def _unique_strings(items) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = _clean_text(item)
        key = text.casefold() if text is not None else None
        if text is None or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _bounded_score(value: object, *, default: int = 0) -> int:
    return clamp_score(value, default=default)


def _positive_price(value: object) -> float | None:
    price = finite_float(value)
    return price if price is not None and price > 0 else None


def _price_text(value: object) -> str | None:
    price = _positive_price(value)
    return f"{price:.2f}" if price is not None else None


def _display_price(value: object) -> str:
    return _price_text(value) or "缺失"


def _contains_text(value: object, keyword: str) -> bool:
    text = _clean_text(value)
    return text is not None and keyword in text


def build_stock_overview(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
    events: StockEventSummary,
) -> StockOverview:
    quote = analysis.quote
    scores = _overview_scores(analysis, fund_flow, order_pressure, events)
    main_conflict = _quality_adjusted_main_conflict(analysis, fund_flow, order_pressure)
    return StockOverview(
        symbol=f"{quote.code}.{quote.market}",
        code=quote.code,
        market=quote.market,
        name=quote.name,
        total_score=scores.total_score,
        total_level=score_level(scores.total_score),
        main_conflict=main_conflict,
        beginner_takeaways=_beginner_takeaways(analysis, main_conflict),
        key_prices=_key_prices(analysis),
        risk_triggers=_risk_triggers(analysis, order_pressure),
        factors=scores.factors,
        action_advice=analysis.action_advice,
        updated_at=quote.timestamp,
    )


def _overview_scores(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
    events: StockEventSummary,
) -> OverviewScores:
    factors = _overview_factors(analysis, fund_flow, order_pressure, events)
    factor_score = round(sum(_bounded_score(item.score) for item in factors) / len(factors))
    signal_quality_score = _signal_quality_score(analysis)
    total_score = _quality_adjusted_total_score(analysis, factor_score, signal_quality_score)
    return OverviewScores(factors=factors, factor_score=factor_score, signal_quality_score=signal_quality_score, total_score=total_score)


def _overview_factors(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
    events: StockEventSummary,
) -> list[FactorScore]:
    return [
        _technical_factor(analysis),
        _fund_factor(fund_flow),
        _fundamental_factor(analysis),
        _event_factor(events),
        _risk_factor(analysis, order_pressure),
    ]


def _signal_quality_score(analysis: AnalysisResult) -> int:
    confidence = _bounded_score(analysis.signal_snapshot.confidence)
    data_quality_score = _bounded_score(analysis.data_quality.score)
    return clamp_score(confidence * 0.7 + data_quality_score * 0.3, round_value=True)


def _quality_adjusted_total_score(analysis: AnalysisResult, factor_score: int, signal_quality_score: int) -> int:
    clean_factor_score = _bounded_score(factor_score)
    clean_signal_quality_score = _bounded_score(signal_quality_score)
    data_quality_score = _bounded_score(analysis.data_quality.score)
    total_score = clamp_score(clean_factor_score * 0.68 + clean_signal_quality_score * 0.32, round_value=True)
    if data_quality_score < DATA_QUALITY_CAP_THRESHOLD:
        return min(total_score, round((clean_factor_score + data_quality_score) / 2))
    return total_score


def _quality_adjusted_main_conflict(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
) -> str:
    main_conflict = _main_conflict(analysis, fund_flow, order_pressure)
    if _bounded_score(analysis.data_quality.score) < DATA_QUALITY_CAP_THRESHOLD and not main_conflict.startswith("数据质量"):
        level = _clean_text(analysis.data_quality.level) or "偏低"
        return f"数据质量{level}，{main_conflict}"
    return main_conflict


def _beginner_takeaways(analysis: AnalysisResult, main_conflict: str) -> list[str]:
    confidence = _bounded_score(getattr(analysis.signal_snapshot, "confidence", None))
    data_quality_level = _clean_text(getattr(analysis.data_quality, "level", None)) or "未知"
    action = _clean_text(getattr(analysis.action_advice, "action", None)) or "观察"
    action_confidence = _bounded_score(getattr(analysis.action_advice, "confidence", None))
    return _unique_strings(
        [
            f"本次信号可信度 {confidence}%，结论已按数据质量 {data_quality_level} 自动降权。",
            _support_resistance_takeaway(analysis),
            f"当前建议是「{action}」，信心 {action_confidence}%。",
            main_conflict,
        ]
    )


def _support_resistance_takeaway(analysis: AnalysisResult) -> str:
    support_value, resistance_value = _normalized_support_resistance(analysis)
    support = _price_text(support_value)
    resistance = _price_text(resistance_value)
    if support and resistance:
        return f"先看 {support} 支撑是否守住，再看 {resistance} 压力能否放量突破。"
    if support:
        return f"先看 {support} 支撑是否守住，压力位待重算。"
    if resistance:
        return f"支撑位待重算，再看 {resistance} 压力能否放量突破。"
    return "支撑/压力位待重算，先降低关键价位判断权重。"


def _key_prices(analysis: AnalysisResult) -> list[KeyPriceLevel]:
    support, resistance = _normalized_support_resistance(analysis)
    candidates = [
        ("支撑位", support, "跌破后当前趋势判断需要降级。"),
        ("压力位", resistance, "放量突破后才算右侧确认。"),
        ("5日线", _positive_price(analysis.ma5), "短线强弱的第一观察线。"),
        ("20日线", _positive_price(analysis.ma20), "波段风控的重要参考线。"),
    ]
    return [KeyPriceLevel(label=label, price=price, note=note) for label, price, note in candidates if price is not None]


def _normalized_support_resistance(analysis: AnalysisResult) -> tuple[float | None, float | None]:
    support = _positive_price(analysis.support)
    resistance = _positive_price(analysis.resistance)
    if support is not None and resistance is not None and support > resistance:
        return resistance, support
    return support, resistance


def _technical_factor(analysis: AnalysisResult) -> FactorScore:
    trend_score = _bounded_score(analysis.trend_score)
    trend_label = _clean_text(analysis.trend_label) or "趋势待确认"
    evidence = _unique_strings(
        [
            f"趋势评分 {trend_score}/100，状态为{trend_label}。",
            f"现价 {_display_price(analysis.quote.price)}，5日线 {_display_price(analysis.ma5)}，20日线 {_display_price(analysis.ma20)}。",
            *(_signal_contribution_evidence(item) for item in _top_signal_contributions(analysis)),
        ]
    )
    return FactorScore(
        name="技术面",
        score=trend_score,
        level=score_level(trend_score),
        summary=trend_label,
        evidence=evidence,
    )


def _top_signal_contributions(analysis: AnalysisResult) -> list[object]:
    snapshot = analysis.signal_snapshot
    positive = list(getattr(snapshot, "positive", []) or [])
    negative = list(getattr(snapshot, "negative", []) or [])
    return [*positive[:2], *negative[:2]]


def _signal_contribution_evidence(item: object) -> str | None:
    name = _clean_text(getattr(item, "name", None))
    reason = _clean_text(getattr(item, "reason", None))
    if name is None and reason is None:
        return None
    impact = finite_float(getattr(item, "impact", None))
    impact_value = int(impact) if impact is not None else 0
    return f"{name or '信号'}{impact_value:+d}：{reason or '原因待补充'}"


def _fund_factor(fund_flow: FundFlowAnalysis) -> FactorScore:
    score = _bounded_score(fund_flow.overall_score)
    return FactorScore(
        name="资金面",
        score=score,
        level=score_level(score),
        summary=_clean_text(fund_flow.price_volume_relation) or "资金流向待确认",
        evidence=_unique_strings(getattr(item, "summary", None) for item in (getattr(fund_flow, "windows", []) or [])),
        missing_data=[] if getattr(fund_flow, "available", False) else ["逐笔大单/特大单资金流"],
    )


def _fundamental_factor(analysis: AnalysisResult) -> FactorScore:
    parts = _fundamental_parts(analysis)
    evidence = _unique_strings(item.evidence for item in parts)
    missing = _unique_strings(item.missing_data for item in parts)
    score = FUNDAMENTAL_BASE_SCORE + sum(item.score_adjustment for item in parts)
    score = clamp_score(score)
    return FactorScore(
        name="基本面",
        score=score,
        level=score_level(score),
        summary="估值字段可用" if evidence else "基础财务数据待接入",
        evidence=evidence or ["当前只有行情字段，财报指标待接入。"],
        missing_data=missing,
    )


def _fundamental_parts(analysis: AnalysisResult) -> list[FundamentalFieldResult]:
    quote = analysis.quote
    return [
        _pe_part(quote.pe),
        _pb_part(quote.pb),
        _market_cap_part(quote.market_cap),
        _industry_part(analysis),
    ]


def _pe_part(pe: float | None) -> FundamentalFieldResult:
    return _valuation_metric_part(pe, VALUATION_METRIC_SPECS["pe"])


def _pb_part(pb: float | None) -> FundamentalFieldResult:
    return _valuation_metric_part(pb, VALUATION_METRIC_SPECS["pb"])


def _market_cap_part(market_cap: float | None) -> FundamentalFieldResult:
    value = finite_float(market_cap)
    if value is None or value <= 0:
        return FundamentalFieldResult(missing_data="市值")
    return FundamentalFieldResult(evidence=f"总市值 {value / 100000000:.1f} 亿")


def _industry_part(analysis: AnalysisResult) -> FundamentalFieldResult:
    industry = _clean_text(getattr(analysis.stock_profile, "industry", None)) if analysis.stock_profile else None
    if industry:
        return FundamentalFieldResult(evidence=f"行业：{industry}")
    return FundamentalFieldResult(missing_data="行业/财务明细")


def _valuation_adjustment(value: float, low_threshold: float, high_threshold: float, adjustment: int) -> int:
    if value < low_threshold:
        return adjustment
    if value > high_threshold:
        return -adjustment
    return 0


def _valuation_metric_part(value: float | None, spec: ValuationMetricSpec) -> FundamentalFieldResult:
    clean_value = finite_float(value)
    if clean_value is None or clean_value <= 0:
        return FundamentalFieldResult(missing_data=spec.missing_data)
    return FundamentalFieldResult(
        score_adjustment=_valuation_adjustment(clean_value, spec.low_threshold, spec.high_threshold, spec.adjustment),
        evidence=f"{spec.label} {clean_value:.2f}",
    )


def _event_factor(events: StockEventSummary) -> FactorScore:
    event_items = _unique_event_items(getattr(events, "events", []))
    risk_count = sum(1 for item in event_items if _event_level(item) == "风险")
    positive_count = sum(1 for item in event_items if _event_level(item) == "积极")
    score = clamp_score(58 + positive_count * 8 - risk_count * 10)
    notes = _unique_strings(getattr(events, "notes", []) or [])
    return FactorScore(
        name="事件面",
        score=score,
        level=score_level(score),
        summary="事件偏积极" if positive_count > risk_count else "事件需观察" if event_items else "暂无事件",
        evidence=_unique_strings(_event_evidence(item) for item in event_items[:4]),
        missing_data=["公告全文", "研报摘要", "龙虎榜"] if notes else [],
    )


def _event_level(item: object) -> str | None:
    return _clean_text(getattr(item, "level", None))


def _event_evidence(item: object) -> str | None:
    title = _clean_text(getattr(item, "title", None))
    if title is None:
        return None
    category = _clean_text(getattr(item, "category", None)) or "事件"
    return f"{category}：{title}"


def _unique_event_items(events) -> list[object]:
    seen: set[tuple[str, str, str]] = set()
    result: list[object] = []
    for item in events or []:
        title = _clean_text(getattr(item, "title", None))
        if title is None:
            continue
        category = _clean_text(getattr(item, "category", None)) or "事件"
        level = _clean_text(getattr(item, "level", None)) or ""
        key = (
            category.casefold(),
            title.casefold(),
            level.casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _risk_factor(analysis: AnalysisResult, order_pressure: OrderPressure) -> FactorScore:
    data_quality_score = _bounded_score(analysis.data_quality.score)
    confidence = _bounded_score(analysis.signal_snapshot.confidence)
    risk_level = _clean_text(analysis.risk_level) or "风险待确认"
    risk_score = 100 - data_quality_score
    if risk_level == "高风险":
        risk_score += 30
    elif risk_level == "中等风险":
        risk_score += 18
    if _contains_text(order_pressure.pressure_level, "卖压"):
        risk_score += 10
    if confidence < LOW_SIGNAL_CONFIDENCE_THRESHOLD:
        risk_score += 10
    score = clamp_score(100 - risk_score)
    return FactorScore(
        name="风险面",
        score=score,
        level=score_level(score),
        summary=risk_level,
        evidence=_unique_strings(
            [
                analysis.action_advice.reason,
                order_pressure.summary,
                f"信号可信度 {confidence}%，数据质量 {data_quality_score} 分。",
            ]
        ),
        missing_data=[] if getattr(order_pressure, "available", False) else ["实时五档盘口"],
    )


def _main_conflict(analysis: AnalysisResult, fund_flow: FundFlowAnalysis, order_pressure: OrderPressure) -> str:
    context = MainConflictContext(analysis=analysis, fund_flow=fund_flow, order_pressure=order_pressure)
    for rule in MAIN_CONFLICT_RULES:
        if rule.matches(context):
            return rule.message
    return "当前主要矛盾是趋势确认和风险控制，优先观察关键价位是否有效。"


def _analysis_data_quality_score(analysis: AnalysisResult) -> int:
    return _bounded_score(getattr(getattr(analysis, "data_quality", None), "score", None))


def _analysis_signal_confidence(analysis: AnalysisResult) -> int:
    return _bounded_score(getattr(getattr(analysis, "signal_snapshot", None), "confidence", None))


def _analysis_trend_score(analysis: AnalysisResult) -> int:
    return _bounded_score(getattr(analysis, "trend_score", None))


def _fund_flow_score(fund_flow: FundFlowAnalysis) -> int:
    return _bounded_score(getattr(fund_flow, "overall_score", None))


def _fund_flow_available(fund_flow: FundFlowAnalysis) -> bool:
    return bool(getattr(fund_flow, "available", True))


def _has_weak_data_quality(context: MainConflictContext) -> bool:
    return _analysis_data_quality_score(context.analysis) < WEAK_DATA_QUALITY_THRESHOLD


def _has_low_signal_confidence(context: MainConflictContext) -> bool:
    return _analysis_signal_confidence(context.analysis) < LOW_SIGNAL_CONFIDENCE_THRESHOLD


def _has_weak_trend_strong_fund_flow(context: MainConflictContext) -> bool:
    return (
        _fund_flow_available(context.fund_flow)
        and _analysis_trend_score(context.analysis) < WEAK_TREND_THRESHOLD
        and _fund_flow_score(context.fund_flow) >= STRONG_FUND_FLOW_THRESHOLD
    )


def _has_strong_trend_weak_fund_flow(context: MainConflictContext) -> bool:
    return (
        _fund_flow_available(context.fund_flow)
        and _analysis_trend_score(context.analysis) >= STRONG_TREND_THRESHOLD
        and _fund_flow_score(context.fund_flow) < WEAK_FUND_FLOW_THRESHOLD
    )


def _has_order_sell_pressure(context: MainConflictContext) -> bool:
    return _contains_text(context.order_pressure.pressure_level, "卖压")


MAIN_CONFLICT_RULES = (
    MainConflictRule(
        "weak_data_quality",
        "数据质量较弱，当前所有买卖点、做T和规则命中都只能低置信观察。",
        _has_weak_data_quality,
    ),
    MainConflictRule(
        "low_signal_confidence",
        "趋势证据和数据可信度都不够强，先降低操作频率，等待更清晰的确认。",
        _has_low_signal_confidence,
    ),
    MainConflictRule(
        "weak_trend_strong_fund_flow",
        "资金面有尝试修复，但技术趋势仍偏弱，先等价格重新站稳短期均线。",
        _has_weak_trend_strong_fund_flow,
    ),
    MainConflictRule(
        "strong_trend_weak_fund_flow",
        "技术面尚可，但资金跟随不足，突破信号需要继续确认。",
        _has_strong_trend_weak_fund_flow,
    ),
    MainConflictRule(
        "sell_pressure",
        "盘口或价格位置显示上方压力，短线不宜追高。",
        _has_order_sell_pressure,
    ),
)


def _risk_triggers(analysis: AnalysisResult, order_pressure: OrderPressure) -> list[str]:
    triggers = [
        _support_break_trigger(analysis),
        _ma20_break_trigger(analysis),
        "数据质量降为“一般”以下",
    ]
    if _contains_text(order_pressure.pressure_level, "卖压"):
        triggers.append("盘口卖压持续强于买盘")
    if _unique_strings(getattr(analysis.data_quality, "anomalies", []) or []):
        triggers.append("行情字段异常未修复")
    return _unique_strings(triggers)


def _support_break_trigger(analysis: AnalysisResult) -> str:
    support, _ = _normalized_support_resistance(analysis)
    support = _price_text(support)
    return f"有效跌破支撑位 {support}" if support else "支撑位缺失，先等待有效价位重算"


def _ma20_break_trigger(analysis: AnalysisResult) -> str:
    ma20 = _price_text(analysis.ma20)
    return f"收盘跌破20日线 {ma20}" if ma20 else "20日线缺失，先等待均线重算"
