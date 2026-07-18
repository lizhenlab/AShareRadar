from __future__ import annotations

from app.models.schemas import AbnormalEventSummary, AnalysisResult, FundFlowAnalysis, OrderPressure, RuleMatch
from app.services.stock_rule_contracts import (
    LEVEL_CAUTIOUS,
    LEVEL_NEUTRAL,
    LEVEL_RISK,
    LEVEL_WATCH,
    RULE_PARAMETERS_BY_ID,
    STATUS_MATCHED,
    STATUS_MISSED,
    FundTechDivergenceState,
    SupportReboundState,
    _rule_match_fields,
)
from app.services.stock_rule_values import (
    _non_negative_score,
    _positive_price_level,
    _price_level_evidence,
    _rule_confidence,
    _score_evidence,
    _status_from_flags,
)


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
    config = RULE_PARAMETERS_BY_ID["support_rebound_watch"]
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
    config = RULE_PARAMETERS_BY_ID["support_rebound_watch"]
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
        _score_evidence("量价热度评分（衍生）", fund_flow.overall_score),
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
        missing_data.append("量价热度评分（衍生）")
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
            "等趋势和量价热度（衍生）至少一方完成修复后再提高信号权重。",
            "出现背离时，不把单一强项当作完整买卖依据。",
        ],
        invalidation="趋势评分和量价热度评分（衍生）重新回到同向区间。",
        evidence=evidence,
        missing_data=_fund_tech_divergence_missing_data(analysis, fund_flow),
    )


def _fund_tech_divergence_state(trend_score: int, fund_score: int) -> FundTechDivergenceState:
    config = RULE_PARAMETERS_BY_ID["fund_tech_divergence"]
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
        _score_evidence("量价热度评分（衍生）", fund_flow.overall_score),
        f"盘口 {order_pressure.pressure_level}",
    ]


def _fund_tech_divergence_missing_data(analysis: AnalysisResult, fund_flow: FundFlowAnalysis) -> list[str]:
    missing_data = []
    if _non_negative_score(analysis.trend_score) is None:
        missing_data.append("趋势评分")
    if _non_negative_score(fund_flow.overall_score) is None:
        missing_data.append("量价热度评分（衍生）")
    if not fund_flow.available:
        missing_data.append("逐笔资金流")
    return missing_data
