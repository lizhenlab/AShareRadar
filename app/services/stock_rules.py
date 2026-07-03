from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from app.models.schemas import (
    AbnormalEventSummary,
    AbnormalEventItem,
    AnalysisResult,
    FundFlowAnalysis,
    OrderPressure,
    RuleDefinition,
    RuleMatch,
    StockRuleMatchSummary,
    ValuationAnalysis,
)
from app.services.stock_abnormal_context import completed_kline_rows, current_volume_metrics
from app.utils.market_data import finite_float


RULE_VERSION = "rules.v2"
SCORE_VERSION = "score.v2"
STATUS_MATCHED = "命中"
STATUS_CLOSE = "接近"
STATUS_MISSED = "未触发"
LEVEL_RISK = "风险"
LEVEL_POSITIVE = "积极"
LEVEL_WATCH = "观察"
LEVEL_CAUTIOUS = "谨慎"
LEVEL_NEUTRAL = "中性"
QUALITY_GATE_PASS_SCORE = 70
QUALITY_GATE_WEAK_SCORE = 50
QUALITY_GATE_MATCHED_PENALTY = 18
QUALITY_GATE_CLOSE_PENALTY = 10
QUALITY_GATE_MISSED_PENALTY = 6
QUALITY_GATE_RISK_MULTIPLIER = 0.85
QUALITY_GATE_MATCHED_CONFIDENCE_FLOOR = 28
QUALITY_GATE_CLOSE_CONFIDENCE_FLOOR = 24
QUALITY_GATE_MISSED_CONFIDENCE_FLOOR = 20
RULE_CONFIDENCE = {
    "volume_breakout_20d": {
        STATUS_MATCHED: 78,
        STATUS_CLOSE: 56,
        STATUS_MISSED: 35,
    },
    "break_ma20_risk": {
        STATUS_MATCHED: 82,
        STATUS_CLOSE: 58,
        STATUS_MISSED: 38,
    },
    "support_rebound_watch": {
        STATUS_MATCHED: 72,
        STATUS_CLOSE: 54,
        STATUS_MISSED: 32,
    },
    "fund_tech_divergence": {
        STATUS_MATCHED: 74,
        STATUS_CLOSE: 55,
        STATUS_MISSED: 34,
    },
    "high_valuation_chase_risk": {
        STATUS_MATCHED: 76,
        STATUS_CLOSE: 55,
        STATUS_MISSED: 30,
    },
    "abnormal_risk_event": {
        STATUS_MATCHED: 78,
        STATUS_CLOSE: 52,
        STATUS_MISSED: 28,
    },
}
STATUS_SORT_RANK = {STATUS_MATCHED: 0, STATUS_CLOSE: 1, STATUS_MISSED: 2}
LEVEL_SORT_RANK = {
    LEVEL_RISK: 0,
    LEVEL_POSITIVE: 1,
    LEVEL_WATCH: 2,
    LEVEL_CAUTIOUS: 3,
    LEVEL_NEUTRAL: 4,
}
RuleParameter: TypeAlias = float | int | str
RULE_CONFIG: dict[str, dict[str, RuleParameter]] = {
    "volume_breakout_20d": {"near_breakout_pct": 0.985, "volume_ratio": 1.35, "window": 20},
    "break_ma20_risk": {"trend_score": 50, "near_ma20_pct": 1.015},
    "support_rebound_watch": {"near_support_pct": 1.03, "fund_score": 58},
    "fund_tech_divergence": {"trend_weak": 48, "trend_strong": 65, "fund_strong": 62, "fund_weak": 48, "gap": 18},
    "high_valuation_chase_risk": {"trend_hit": 68, "trend_close": 62, "valuation_hit": 45, "valuation_close": 52},
    "abnormal_risk_event": {"risk_event_min": 1},
}


@dataclass(frozen=True)
class VolumeBreakoutState:
    near_breakout: bool
    enough_volume: bool


@dataclass(frozen=True)
class BreakMa20State:
    broken: bool
    close: bool


@dataclass(frozen=True)
class FundTechDivergenceState:
    positive_divergence: bool
    negative_divergence: bool
    gap_reached: bool


@dataclass(frozen=True)
class RuleMatchContext:
    analysis: AnalysisResult
    fund_flow: FundFlowAnalysis
    order_pressure: OrderPressure
    valuation: ValuationAnalysis
    abnormal_events: AbnormalEventSummary
    latest_high_20: float
    volume_ratio: float | None


@dataclass(frozen=True)
class RuleSpec:
    id: str
    name: str
    category: str
    description: str
    beginner_hint: str
    evaluate: Callable[[RuleMatchContext], RuleMatch]

    def definition(self) -> RuleDefinition:
        return RuleDefinition(
            id=self.id,
            name=self.name,
            category=self.category,
            description=self.description,
            beginner_hint=self.beginner_hint,
            version=RULE_VERSION,
            parameters=dict(RULE_CONFIG[self.id]),
        )


@dataclass(frozen=True)
class QualityGateContext:
    match: RuleMatch
    score: int
    level: str


@dataclass(frozen=True)
class QualityGateDecision:
    status: str
    level: str
    confidence: int


@dataclass(frozen=True)
class HighValuationChaseState:
    hit: bool
    close: bool


@dataclass(frozen=True)
class SupportReboundState:
    has_support: bool
    near_support: bool
    has_rebound: bool
    has_risk_event: bool


def _evaluate_volume_breakout(context: RuleMatchContext) -> RuleMatch:
    return _rule_volume_breakout(context.analysis, context.latest_high_20, context.volume_ratio)


def _evaluate_break_ma20(context: RuleMatchContext) -> RuleMatch:
    return _rule_break_ma20(context.analysis)


def _evaluate_support_rebound(context: RuleMatchContext) -> RuleMatch:
    return _rule_support_rebound(context.analysis, context.fund_flow, context.abnormal_events)


def _evaluate_fund_tech_divergence(context: RuleMatchContext) -> RuleMatch:
    return _rule_fund_tech_divergence(context.analysis, context.fund_flow, context.order_pressure)


def _evaluate_high_valuation_chase(context: RuleMatchContext) -> RuleMatch:
    return _rule_high_valuation_chase(context.analysis, context.valuation)


def _evaluate_abnormal_risk(context: RuleMatchContext) -> RuleMatch:
    return _rule_abnormal_risk(context.analysis, context.abnormal_events)


RULE_SPECS = (
    RuleSpec(
        id="volume_breakout_20d",
        name="放量突破20日高点",
        category="趋势",
        description="价格接近或突破近20日高点，同时量能明显高于近5日均量。",
        beginner_hint="这是右侧确认信号，重点看突破后是否站稳，而不是盘中一冲就追。",
        evaluate=_evaluate_volume_breakout,
    ),
    RuleSpec(
        id="break_ma20_risk",
        name="跌破20日线风险",
        category="风控",
        description="现价低于20日均线且趋势评分偏弱。",
        beginner_hint="20日线是波段风控线，跌破后先降低乐观预期。",
        evaluate=_evaluate_break_ma20,
    ),
    RuleSpec(
        id="support_rebound_watch",
        name="支撑位止跌观察",
        category="买点观察",
        description="价格接近支撑位，下影或资金表现出现承接迹象。",
        beginner_hint="这是观察信号，不是越跌越买；必须有止跌证据。",
        evaluate=_evaluate_support_rebound,
    ),
    RuleSpec(
        id="fund_tech_divergence",
        name="资金技术背离",
        category="资金",
        description="趋势与资金评分出现明显分歧。",
        beginner_hint="分歧阶段不要只看一个指标，等待价格或资金给出一致方向。",
        evaluate=_evaluate_fund_tech_divergence,
    ),
    RuleSpec(
        id="high_valuation_chase_risk",
        name="高估值追高风险",
        category="估值",
        description="趋势强但估值压力偏高，容易出现波动放大。",
        beginner_hint="强势股也需要风控线，估值越贵越不能忽略失效条件。",
        evaluate=_evaluate_high_valuation_chase,
    ),
    RuleSpec(
        id="abnormal_risk_event",
        name="风险异动降级",
        category="事件",
        description="出现放量下跌、跌停附近、长上影等风险异动。",
        beginner_hint="风险异动先解释原因，再决定是否继续观察。",
        evaluate=_evaluate_abnormal_risk,
    ),
)
RULE_SPEC_BY_ID = {spec.id: spec for spec in RULE_SPECS}
RULE_SORT_INDEX = {spec.id: index for index, spec in enumerate(RULE_SPECS)}


def rule_definitions() -> list[RuleDefinition]:
    return [spec.definition() for spec in RULE_SPECS]


def _rule_match_fields(rule_id: str) -> dict[str, str]:
    spec = RULE_SPEC_BY_ID[rule_id]
    return {
        "rule_id": spec.id,
        "name": spec.name,
        "category": spec.category,
        "rule_version": RULE_VERSION,
        "score_version": SCORE_VERSION,
    }


def build_rule_match_summary(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
    valuation: ValuationAnalysis,
    abnormal_events: AbnormalEventSummary,
) -> StockRuleMatchSummary:
    context = _rule_match_context(analysis, fund_flow, order_pressure, valuation, abnormal_events)
    matches = _sorted_rule_matches(_raw_rule_matches(context), analysis)
    quote = analysis.quote
    return StockRuleMatchSummary(
        symbol=f"{quote.code}.{quote.market}",
        updated_at=quote.timestamp,
        matched_count=sum(1 for item in matches if item.status == STATUS_MATCHED),
        top_level=_summary_top_level(matches),
        matches=matches,
        definitions=rule_definitions(),
    )


def _rule_match_context(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
    valuation: ValuationAnalysis,
    abnormal_events: AbnormalEventSummary,
) -> RuleMatchContext:
    rows = analysis.klines[-25:]
    completed_rows = completed_kline_rows(analysis.quote, rows)
    latest_high_20 = _latest_completed_high(
        completed_rows,
        int(RULE_CONFIG["volume_breakout_20d"]["window"]),
    )
    volume_metrics = current_volume_metrics(analysis.quote, rows)
    return RuleMatchContext(
        analysis=analysis,
        fund_flow=fund_flow,
        order_pressure=order_pressure,
        valuation=valuation,
        abnormal_events=abnormal_events,
        latest_high_20=latest_high_20,
        volume_ratio=volume_metrics.volume_ratio,
    )


def _latest_completed_high(rows: list, window: int) -> float:
    if window <= 0 or len(rows) < window:
        return 0
    highs = [_positive_price_level(item.high) for item in rows[-window:]]
    valid_highs = [item for item in highs if item is not None]
    return max(valid_highs) if len(valid_highs) == window else 0


def _raw_rule_matches(context: RuleMatchContext) -> list[RuleMatch]:
    return [spec.evaluate(context) for spec in RULE_SPECS]


def _sorted_rule_matches(matches: list[RuleMatch], analysis: AnalysisResult) -> list[RuleMatch]:
    return sorted((_apply_quality_gate(item, analysis) for item in matches), key=_rule_sort_key)


def _summary_top_level(matches: list[RuleMatch]) -> str:
    if _has_hit_level(matches, LEVEL_RISK):
        return LEVEL_RISK
    if _has_hit_level(matches, LEVEL_POSITIVE):
        return LEVEL_POSITIVE
    return LEVEL_WATCH


def _has_hit_level(matches: list[RuleMatch], level: str) -> bool:
    return any(item.level == level and item.status == STATUS_MATCHED for item in matches)


def _apply_quality_gate(match: RuleMatch, analysis: AnalysisResult) -> RuleMatch:
    context = QualityGateContext(match=match, score=analysis.data_quality.score, level=analysis.data_quality.level)
    if _quality_gate_not_needed(context):
        return match
    decision = _quality_gate_decision(context)
    return _copy_with_quality_gate(match, decision, _quality_gate_reason(context))


def _quality_gate_not_needed(context: QualityGateContext) -> bool:
    return context.score >= QUALITY_GATE_PASS_SCORE


def _quality_gate_decision(context: QualityGateContext) -> QualityGateDecision:
    if context.match.level == LEVEL_RISK:
        return _risk_quality_gate_decision(context)
    if context.match.status == STATUS_MATCHED:
        return QualityGateDecision(
            status=STATUS_CLOSE,
            level=_quality_gate_level(context),
            confidence=_reduced_confidence(
                context.match.confidence,
                QUALITY_GATE_MATCHED_PENALTY,
                QUALITY_GATE_MATCHED_CONFIDENCE_FLOOR,
            ),
        )
    if context.match.status == STATUS_CLOSE:
        status = STATUS_MISSED if context.score < QUALITY_GATE_WEAK_SCORE else STATUS_CLOSE
        return QualityGateDecision(
            status=status,
            level=_quality_gate_level(context),
            confidence=_reduced_confidence(
                context.match.confidence,
                QUALITY_GATE_CLOSE_PENALTY,
                QUALITY_GATE_CLOSE_CONFIDENCE_FLOOR,
            ),
        )
    return QualityGateDecision(
        status=context.match.status,
        level=_quality_gate_level(context),
        confidence=_reduced_confidence(
            context.match.confidence,
            QUALITY_GATE_MISSED_PENALTY,
            QUALITY_GATE_MISSED_CONFIDENCE_FLOOR,
        ),
    )


def _risk_quality_gate_decision(context: QualityGateContext) -> QualityGateDecision:
    return QualityGateDecision(
        status=context.match.status,
        level=context.match.level,
        confidence=max(
            QUALITY_GATE_MISSED_CONFIDENCE_FLOOR,
            round(context.match.confidence * QUALITY_GATE_RISK_MULTIPLIER),
        ),
    )


def _quality_gate_level(context: QualityGateContext) -> str:
    if context.match.level == LEVEL_CAUTIOUS:
        return LEVEL_CAUTIOUS
    return LEVEL_CAUTIOUS if context.match.level == LEVEL_POSITIVE and context.score < QUALITY_GATE_WEAK_SCORE else LEVEL_WATCH


def _reduced_confidence(confidence: int, penalty: int, floor: int) -> int:
    return max(floor, confidence - penalty)


def _quality_gate_reason(context: QualityGateContext) -> str:
    return f"数据质量{context.level}，该规则结论已降权。"


def _copy_with_quality_gate(match: RuleMatch, decision: QualityGateDecision, reason: str) -> RuleMatch:
    return match.model_copy(
        update={
            "status": decision.status,
            "level": decision.level,
            "confidence": decision.confidence,
            "evidence": [*match.evidence, reason],
        }
    )


def _rule_sort_key(item: RuleMatch) -> tuple[int, int, int, int]:
    status_rank = STATUS_SORT_RANK.get(item.status, 3)
    level_rank = LEVEL_SORT_RANK.get(item.level, 5)
    rule_rank = RULE_SORT_INDEX.get(item.rule_id, len(RULE_SORT_INDEX))
    return status_rank, level_rank, -item.confidence, rule_rank


def _status_from_flags(hit: bool, close: bool) -> str:
    if hit:
        return STATUS_MATCHED
    if close:
        return STATUS_CLOSE
    return STATUS_MISSED


def _rule_volume_breakout(analysis: AnalysisResult, latest_high_20: float, volume_ratio: float | None) -> RuleMatch:
    quote = analysis.quote
    state = _volume_breakout_state(quote.price, latest_high_20, volume_ratio)
    status = _volume_breakout_status(state)
    evidence = _volume_breakout_evidence(quote.price, latest_high_20, volume_ratio)
    return RuleMatch(
        **_rule_match_fields("volume_breakout_20d"),
        status=status,
        level=_volume_breakout_level(status),
        confidence=_rule_confidence("volume_breakout_20d", status),
        reason="；".join(evidence),
        actions=[
            "只把站稳压力位后的回踩作为确认点。",
            "突破当日若放量过猛，次日承接更关键。",
        ],
        invalidation=f"跌回压力位 {analysis.resistance:.2f} 下方或量能快速萎缩。",
        evidence=evidence,
        missing_data=_volume_breakout_missing_data(quote.price, latest_high_20, volume_ratio),
    )


def _volume_breakout_state(price: float, latest_high_20: float, volume_ratio: float | None) -> VolumeBreakoutState:
    config = RULE_CONFIG["volume_breakout_20d"]
    current_price = _positive_price_level(price)
    high_threshold = _positive_price_level(latest_high_20)
    clean_volume_ratio = _positive_metric(volume_ratio)
    return VolumeBreakoutState(
        near_breakout=current_price is not None
        and high_threshold is not None
        and current_price >= high_threshold * float(config["near_breakout_pct"]),
        enough_volume=clean_volume_ratio is not None and clean_volume_ratio >= float(config["volume_ratio"]),
    )


def _volume_breakout_status(state: VolumeBreakoutState) -> str:
    return _status_from_flags(state.near_breakout and state.enough_volume, state.near_breakout or state.enough_volume)


def _volume_breakout_level(status: str) -> str:
    return LEVEL_POSITIVE if status == STATUS_MATCHED else LEVEL_WATCH


def _volume_breakout_evidence(price: float, latest_high_20: float, volume_ratio: float | None) -> list[str]:
    evidence = [f"{_price_level_evidence('现价', price)} / {_price_level_evidence('20日高点', latest_high_20)}"]
    clean_volume_ratio = _positive_metric(volume_ratio)
    if clean_volume_ratio is not None:
        evidence.append(f"量比估算 {clean_volume_ratio:.2f}")
    elif volume_ratio is not None:
        evidence.append("量比估算 缺失")
    return evidence


def _volume_breakout_missing_data(price: float, latest_high_20: float, volume_ratio: float | None) -> list[str]:
    missing_data = []
    if _positive_price_level(price) is None:
        missing_data.append("现价")
    if _positive_price_level(latest_high_20) is None:
        missing_data.append("20日高点")
    if _positive_metric(volume_ratio) is None:
        missing_data.append("近5日成交量")
    return missing_data


def _positive_price_level(value: object) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _positive_metric(value: object) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _non_negative_score(value: object) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _positive_score(value: object) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _price_level_evidence(label: str, value: object) -> str:
    price_level = _positive_price_level(value)
    return f"{label} {price_level:.2f}" if price_level is not None else f"{label} 缺失"


def _score_evidence(label: str, value: object, *, positive: bool = False) -> str:
    score = _positive_score(value) if positive else _non_negative_score(value)
    return f"{label} {score:g}" if score is not None else f"{label} 缺失"


def _missing_data_items(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _rule_break_ma20(analysis: AnalysisResult) -> RuleMatch:
    status = _break_ma20_status(_break_ma20_state(analysis))
    evidence = _break_ma20_evidence(analysis)
    return RuleMatch(
        **_rule_match_fields("break_ma20_risk"),
        status=status,
        level=_break_ma20_level(status),
        confidence=_break_ma20_confidence(status),
        reason="，".join(evidence) + "。",
        actions=[
            "跌破后先观察能否快速收回20日线。",
            "若同时跌破支撑位，当前建议需要降级。",
        ],
        invalidation=_break_ma20_invalidation(analysis),
        evidence=evidence,
        missing_data=_break_ma20_missing_data(analysis),
    )


def _break_ma20_state(analysis: AnalysisResult) -> BreakMa20State:
    config = RULE_CONFIG["break_ma20_risk"]
    price = _positive_price_level(analysis.quote.price)
    ma20 = _positive_price_level(analysis.ma20)
    trend_score = _non_negative_score(analysis.trend_score)
    return BreakMa20State(
        broken=price is not None
        and ma20 is not None
        and trend_score is not None
        and price < ma20
        and trend_score < float(config["trend_score"]),
        close=price is not None and ma20 is not None and price < ma20 * float(config["near_ma20_pct"]),
    )


def _break_ma20_status(state: BreakMa20State) -> str:
    return _status_from_flags(state.broken, state.close)


def _break_ma20_level(status: str) -> str:
    return LEVEL_RISK if status == STATUS_MATCHED else LEVEL_WATCH


def _break_ma20_confidence(status: str) -> int:
    return _rule_confidence("break_ma20_risk", status)


def _break_ma20_evidence(analysis: AnalysisResult) -> list[str]:
    return [
        _price_level_evidence("现价", analysis.quote.price),
        _price_level_evidence("20日线", analysis.ma20),
        _score_evidence("趋势评分", analysis.trend_score),
    ]


def _break_ma20_invalidation(analysis: AnalysisResult) -> str:
    ma20 = _positive_price_level(analysis.ma20)
    if ma20 is None:
        return "缺少有效20日线时不启用该风控信号。"
    return f"重新站上20日线 {ma20:.2f} 且趋势评分回到50以上。"


def _break_ma20_missing_data(analysis: AnalysisResult) -> list[str]:
    missing_data = []
    if _positive_price_level(analysis.quote.price) is None:
        missing_data.append("现价")
    if _positive_price_level(analysis.ma20) is None:
        missing_data.append("20日线")
    if _non_negative_score(analysis.trend_score) is None:
        missing_data.append("趋势评分")
    return missing_data


def _rule_support_rebound(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    abnormal_events: AbnormalEventSummary,
) -> RuleMatch:
    state = _support_rebound_state(analysis, fund_flow, abnormal_events)
    status = _support_rebound_status(state)
    evidence = _support_rebound_evidence(analysis, fund_flow, state)
    return RuleMatch(
        **_rule_match_fields("support_rebound_watch"),
        status=status,
        level=_support_rebound_level(status, state),
        confidence=_support_rebound_confidence(status, state),
        reason="，".join(evidence) + "。",
        actions=[
            "只适合作为观察点，等待短周期止跌确认。",
            "若跌破支撑，不做摊低成本式加仓建议。",
        ],
        invalidation=_support_rebound_invalidation(analysis),
        evidence=evidence,
        missing_data=_support_rebound_missing_data(analysis, fund_flow, state),
    )


def _support_rebound_state(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    abnormal_events: AbnormalEventSummary,
) -> SupportReboundState:
    quote = analysis.quote
    config = RULE_CONFIG["support_rebound_watch"]
    price = _positive_price_level(quote.price)
    support_level = _positive_price_level(analysis.support)
    has_support = support_level is not None
    near_support = price is not None and has_support and price <= support_level * float(config["near_support_pct"])
    return SupportReboundState(
        has_support=has_support,
        near_support=near_support,
        has_rebound=_has_rebound_evidence(fund_flow, abnormal_events),
        has_risk_event=_has_risk_event(abnormal_events),
    )


def _has_rebound_evidence(fund_flow: FundFlowAnalysis, abnormal_events: AbnormalEventSummary) -> bool:
    config = RULE_CONFIG["support_rebound_watch"]
    has_lower_shadow = any(
        item.title == "长下影承接" or item.direction == "承接"
        for item in abnormal_events.events
    )
    fund_score = _non_negative_score(fund_flow.overall_score)
    return has_lower_shadow or (fund_score is not None and fund_score >= float(config["fund_score"]))


def _has_risk_event(abnormal_events: AbnormalEventSummary) -> bool:
    return any(item.level == LEVEL_RISK for item in abnormal_events.events)


def _support_rebound_status(state: SupportReboundState) -> str:
    return _status_from_flags(state.near_support and state.has_rebound and not state.has_risk_event, state.near_support)


def _support_rebound_level(status: str, state: SupportReboundState) -> str:
    if status == STATUS_MISSED:
        return LEVEL_NEUTRAL
    return LEVEL_CAUTIOUS if state.has_risk_event else LEVEL_WATCH


def _support_rebound_confidence(status: str, state: SupportReboundState) -> int:
    base = _rule_confidence("support_rebound_watch", status)
    return max(36, base - 12) if state.has_risk_event and status != STATUS_MISSED else base


def _support_rebound_evidence(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    state: SupportReboundState,
) -> list[str]:
    quote = analysis.quote
    evidence = [
        _price_level_evidence("现价", quote.price),
        _price_level_evidence("支撑", analysis.support),
        _score_evidence("资金评分", fund_flow.overall_score),
    ]
    if state.has_rebound:
        evidence.append("存在止跌承接证据")
    if state.has_risk_event:
        evidence.append("同时存在风险异动")
    return evidence


def _support_rebound_invalidation(analysis: AnalysisResult) -> str:
    support_level = _positive_price_level(analysis.support)
    if support_level is not None:
        return f"有效跌破支撑 {support_level:.2f}。"
    return "缺少有效支撑位时不启用该观察信号。"


def _support_rebound_missing_data(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    state: SupportReboundState,
) -> list[str]:
    missing_data = []
    if _positive_price_level(analysis.quote.price) is None:
        missing_data.append("现价")
    if not state.has_support:
        missing_data.append("支撑位")
    if _non_negative_score(fund_flow.overall_score) is None:
        missing_data.append("资金评分")
    return missing_data


def _rule_fund_tech_divergence(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
) -> RuleMatch:
    state = _fund_tech_divergence_state(analysis.trend_score, fund_flow.overall_score)
    status = _fund_tech_divergence_status(state)
    evidence = _fund_tech_divergence_evidence(analysis, fund_flow, order_pressure)
    return RuleMatch(
        **_rule_match_fields("fund_tech_divergence"),
        status=status,
        level=_fund_tech_divergence_level(status, state, order_pressure),
        confidence=_rule_confidence("fund_tech_divergence", status),
        reason="，".join(evidence) + "。",
        actions=[
            "等趋势和资金至少一方完成修复后再提高信号权重。",
            "出现背离时，不把单一强项当作完整买卖依据。",
        ],
        invalidation="趋势评分和资金评分重新回到同向区间。",
        evidence=evidence,
        missing_data=_fund_tech_divergence_missing_data(analysis, fund_flow),
    )


def _fund_tech_divergence_state(trend_score: int, fund_score: int) -> FundTechDivergenceState:
    config = RULE_CONFIG["fund_tech_divergence"]
    clean_trend_score = _non_negative_score(trend_score)
    clean_fund_score = _non_negative_score(fund_score)
    if clean_trend_score is None or clean_fund_score is None:
        return FundTechDivergenceState(False, False, False)
    return FundTechDivergenceState(
        positive_divergence=clean_trend_score < float(config["trend_weak"])
        and clean_fund_score >= float(config["fund_strong"]),
        negative_divergence=clean_trend_score >= float(config["trend_strong"])
        and clean_fund_score < float(config["fund_weak"]),
        gap_reached=abs(clean_trend_score - clean_fund_score) >= float(config["gap"]),
    )


def _fund_tech_divergence_status(state: FundTechDivergenceState) -> str:
    return _status_from_flags(state.positive_divergence or state.negative_divergence, state.gap_reached)


def _fund_tech_divergence_level(status: str, state: FundTechDivergenceState, order_pressure: OrderPressure) -> str:
    has_risk_pressure = state.negative_divergence or "卖压" in order_pressure.pressure_level
    return LEVEL_RISK if status == STATUS_MATCHED and has_risk_pressure else LEVEL_WATCH


def _fund_tech_divergence_evidence(
    analysis: AnalysisResult,
    fund_flow: FundFlowAnalysis,
    order_pressure: OrderPressure,
) -> list[str]:
    return [
        _score_evidence("趋势评分", analysis.trend_score),
        _score_evidence("资金评分", fund_flow.overall_score),
        f"盘口 {order_pressure.pressure_level}",
    ]


def _fund_tech_divergence_missing_data(analysis: AnalysisResult, fund_flow: FundFlowAnalysis) -> list[str]:
    missing_data = []
    if _non_negative_score(analysis.trend_score) is None:
        missing_data.append("趋势评分")
    if _non_negative_score(fund_flow.overall_score) is None:
        missing_data.append("资金评分")
    if not fund_flow.available:
        missing_data.append("逐笔资金流")
    return missing_data


def _rule_confidence(rule_id: str, status: str) -> int:
    return int(RULE_CONFIDENCE[rule_id][status])


def _rule_high_valuation_chase(analysis: AnalysisResult, valuation: ValuationAnalysis) -> RuleMatch:
    state = _high_valuation_chase_state(analysis.trend_score, valuation.score)
    status = _high_valuation_chase_status(state)
    return RuleMatch(
        **_rule_match_fields("high_valuation_chase_risk"),
        status=status,
        level=_high_valuation_chase_level(status),
        confidence=_high_valuation_chase_confidence(status),
        reason=_high_valuation_chase_reason(analysis, valuation),
        actions=[
            "强趋势里更重视失效价，不只用估值逆势判断顶部。",
            "若放量滞涨或跌破5日线，及时降低信号等级。",
        ],
        invalidation="估值评分改善，或趋势从急涨进入健康整理后重新评估。",
        evidence=_high_valuation_chase_evidence(analysis, valuation),
        missing_data=_high_valuation_chase_missing_data(valuation),
    )


def _high_valuation_chase_state(trend_score: int, valuation_score: int) -> HighValuationChaseState:
    config = RULE_CONFIG["high_valuation_chase_risk"]
    clean_trend_score = _non_negative_score(trend_score)
    clean_valuation_score = _positive_score(valuation_score)
    if clean_trend_score is None or clean_valuation_score is None:
        return HighValuationChaseState(False, False)
    return HighValuationChaseState(
        hit=clean_trend_score >= float(config["trend_hit"]) and clean_valuation_score < float(config["valuation_hit"]),
        close=clean_trend_score >= float(config["trend_close"]) and clean_valuation_score < float(config["valuation_close"]),
    )


def _high_valuation_chase_status(state: HighValuationChaseState) -> str:
    return _status_from_flags(state.hit, state.close)


def _high_valuation_chase_level(status: str) -> str:
    return LEVEL_RISK if status == STATUS_MATCHED else LEVEL_WATCH


def _high_valuation_chase_confidence(status: str) -> int:
    return _rule_confidence("high_valuation_chase_risk", status)


def _high_valuation_chase_reason(analysis: AnalysisResult, valuation: ValuationAnalysis) -> str:
    return f"{_score_evidence('趋势评分', analysis.trend_score)}，{_score_evidence('估值评分', valuation.score, positive=True)}。{valuation.summary}"


def _high_valuation_chase_evidence(analysis: AnalysisResult, valuation: ValuationAnalysis) -> list[str]:
    return [
        _score_evidence("趋势评分", analysis.trend_score),
        _score_evidence("估值评分", valuation.score, positive=True),
        valuation.summary,
    ]


def _high_valuation_chase_missing_data(valuation: ValuationAnalysis) -> list[str]:
    missing_data = list(valuation.missing_data)
    if _positive_score(valuation.score) is None:
        missing_data.append("估值评分")
    return _missing_data_items(missing_data)


def _rule_abnormal_risk(analysis: AnalysisResult, abnormal_events: AbnormalEventSummary) -> RuleMatch:
    risk_events = _abnormal_risk_events(abnormal_events)
    status = _abnormal_risk_status(risk_events, abnormal_events)
    evidence = _abnormal_risk_evidence(risk_events, abnormal_events)
    return RuleMatch(
        **_rule_match_fields("abnormal_risk_event"),
        status=status,
        level=LEVEL_RISK if risk_events else LEVEL_WATCH,
        confidence=_abnormal_risk_confidence(status),
        reason=f"{'；'.join(evidence)}。当前风险等级：{analysis.risk_level}。",
        actions=[
            "先解释异动来源，再看关键价位是否失守。",
            "风险异动叠加数据质量异常时，建议结论自动降权。",
        ],
        invalidation="风险异动后的2到3个交易日内重新站稳短期均线且量能恢复正常。",
        evidence=evidence,
    )


def _abnormal_risk_events(abnormal_events: AbnormalEventSummary) -> list[AbnormalEventItem]:
    return [item for item in abnormal_events.events if item.level == LEVEL_RISK]


def _abnormal_risk_status(risk_events: list[AbnormalEventItem], abnormal_events: AbnormalEventSummary) -> str:
    return _status_from_flags(bool(risk_events), bool(abnormal_events.events))


def _abnormal_risk_evidence(risk_events: list[AbnormalEventItem], abnormal_events: AbnormalEventSummary) -> list[str]:
    return [item.title for item in risk_events[:3]] or [abnormal_events.main_signal]


def _abnormal_risk_confidence(status: str) -> int:
    return _rule_confidence("abnormal_risk_event", status)
