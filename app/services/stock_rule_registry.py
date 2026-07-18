from __future__ import annotations

from app.models.schemas import (
    AbnormalEventSummary,
    AnalysisResult,
    FundFlowAnalysis,
    OrderPressure,
    RuleDefinition,
    RuleMatch,
    StockRuleMatchSummary,
    ValuationAnalysis,
)
from app.services.stock_abnormal_context import completed_kline_rows, current_volume_metrics
from app.services.stock_rule_contracts import (
    LEVEL_POSITIVE,
    LEVEL_RISK,
    LEVEL_SORT_RANK,
    LEVEL_WATCH,
    RULE_PARAMETERS_BY_ID,
    STATUS_MATCHED,
    STATUS_SORT_RANK,
    RuleMatchContext,
    rule_spec,
)
from app.services.stock_rule_flow import _rule_fund_tech_divergence, _rule_support_rebound
from app.services.stock_rule_price import _rule_break_ma20, _rule_volume_breakout
from app.services.stock_rule_quality import _apply_quality_gate
from app.services.stock_rule_risk import _rule_abnormal_risk, _rule_high_valuation_chase
from app.services.stock_rule_values import _positive_price_level


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
    rule_spec("volume_breakout_20d", _evaluate_volume_breakout),
    rule_spec("break_ma20_risk", _evaluate_break_ma20),
    rule_spec("support_rebound_watch", _evaluate_support_rebound),
    rule_spec("fund_tech_divergence", _evaluate_fund_tech_divergence),
    rule_spec("high_valuation_chase_risk", _evaluate_high_valuation_chase),
    rule_spec("abnormal_risk_event", _evaluate_abnormal_risk),
)
RULE_SPEC_BY_ID = {spec.id: spec for spec in RULE_SPECS}
RULE_SORT_INDEX = {spec.id: index for index, spec in enumerate(RULE_SPECS)}
RULE_CONFIG = {spec.id: RULE_PARAMETERS_BY_ID[spec.id] for spec in RULE_SPECS}


def rule_definitions() -> list[RuleDefinition]:
    return [spec.definition() for spec in RULE_SPECS]


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


def _rule_sort_key(item: RuleMatch) -> tuple[int, int, int, int]:
    status_rank = STATUS_SORT_RANK.get(item.status, 3)
    level_rank = LEVEL_SORT_RANK.get(item.level, 5)
    rule_rank = RULE_SORT_INDEX.get(item.rule_id, len(RULE_SORT_INDEX))
    return status_rank, level_rank, -item.confidence, rule_rank


def _summary_top_level(matches: list[RuleMatch]) -> str:
    if _has_hit_level(matches, LEVEL_RISK):
        return LEVEL_RISK
    if _has_hit_level(matches, LEVEL_POSITIVE):
        return LEVEL_POSITIVE
    return LEVEL_WATCH


def _has_hit_level(matches: list[RuleMatch], level: str) -> bool:
    return any(item.level == level and item.status == STATUS_MATCHED for item in matches)
