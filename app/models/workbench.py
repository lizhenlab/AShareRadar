"""Composite page-level models used by the stock workbench and market overview APIs."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.analysis import AnalysisResult, FeatureSnapshot, StockInsightBundle

from app.models.market import Quote

from app.models.research import (
    AlphaEvidenceReport,
    ChipAnalysis,
    EventDigestReport,
    EvidenceChainReport,
    FactorLabReport,
    LeadershipReport,
    MarketRegimeReport,
    PeerComparisonReport,
    RiskRadarReport,
    RiskRewardReport,
    SignalValidationReport,
    StockDiagnosis,
    StockQaReport,
    StockReplayAnalysis,
    TStrategyAssistantReport,
    ThemeContextReport,
    TimeframeAlignmentReport,
)

from app.models.user_data import AlertEventItem, AlertRuleItem, ChartMarkSummary, StockNoteItem
from app.utils.audit_time import parse_audit_time
from app.utils.clock import ASHARE_TIMEZONE, utc_now
from app.utils.symbols import is_a_share_stock_code, standard_symbol


class WorkbenchDataWarning(BaseModel):
    component: Literal["advice_snapshot", "chart_marks", "alert_rules", "alert_events", "notes"]
    message: str


class _UpdatedResearchChild(Protocol):
    updated_at: str


class WorkbenchResearchCohort(BaseModel):
    """Decision-time identity shared by every panel in one workbench response."""

    model_config = ConfigDict(extra="forbid")

    requested_symbol: str
    observed_symbol: str
    mode: Literal["interactive_shadow"] = "interactive_shadow"
    decision_time: str
    quote_event_time: str
    signal_date: str
    daily_bar_cutoff: str
    production_effect: Literal["none"] = "none"
    advice_persistence: Literal["disabled"] = "disabled"

    @model_validator(mode="after")
    def validate_identity_and_time(self) -> WorkbenchResearchCohort:
        requested = _canonical_a_share_symbol(self.requested_symbol)
        observed = _canonical_a_share_symbol(self.observed_symbol)
        if requested != observed:
            raise ValueError("workbench requested/observed symbol mismatch")
        decision_time = parse_audit_time(self.decision_time)
        quote_event_time = parse_audit_time(self.quote_event_time)
        if quote_event_time > decision_time:
            raise ValueError("workbench quote event time cannot follow decision time")
        signal = _iso_date(self.signal_date, "signal_date")
        if quote_event_time.astimezone(ASHARE_TIMEZONE).date() != signal:
            raise ValueError("workbench signal date must match quote event market date")
        cutoff = _iso_date(self.daily_bar_cutoff, "daily_bar_cutoff")
        if cutoff > signal:
            raise ValueError("workbench daily bar cutoff cannot follow signal date")
        self.requested_symbol = requested
        self.observed_symbol = observed
        return self


class StockWorkbench(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock-workbench-v2"] = "stock-workbench-v2"
    symbol: str
    generated_at: str
    context_generated_at: str
    research_mode: Literal["interactive_shadow"] = "interactive_shadow"
    production_effect: Literal["none"] = "none"
    diagnosis_production_effect: Literal["none"] = "none"
    research_cohort: WorkbenchResearchCohort
    analysis: AnalysisResult
    insights: StockInsightBundle
    feature_snapshot: FeatureSnapshot
    factor_lab: FactorLabReport
    market_regime: MarketRegimeReport
    signal_validation: SignalValidationReport
    risk_reward: RiskRewardReport
    timeframe_alignment: TimeframeAlignmentReport
    alpha_evidence: AlphaEvidenceReport
    diagnosis: StockDiagnosis
    evidence_chain: EvidenceChainReport
    qa_report: StockQaReport
    event_digest: EventDigestReport
    peer_comparison: PeerComparisonReport
    t_strategy: TStrategyAssistantReport
    risk_radar: RiskRadarReport
    chip_analysis: ChipAnalysis
    leadership: LeadershipReport
    theme_context: ThemeContextReport
    replay: StockReplayAnalysis
    chart_marks: ChartMarkSummary
    alert_rules: list[AlertRuleItem] = Field(default_factory=list)
    alert_events: list[AlertEventItem] = Field(default_factory=list)
    notes: list[StockNoteItem] = Field(default_factory=list)
    local_data_warnings: list[WorkbenchDataWarning] = Field(default_factory=list)
    cache_policy: str = "同一只个股短时间内复用分析结果，避免重复请求外部行情源。"

    @model_validator(mode="after")
    def validate_research_cohort(self) -> StockWorkbench:
        symbol = _canonical_a_share_symbol(self.symbol)
        _validate_workbench_metadata(self, symbol)
        _validate_workbench_quote_binding(self, symbol)
        _validate_workbench_child_symbols(self, symbol)
        _validate_workbench_child_times(self)
        self.symbol = symbol
        return self


def _workbench_symbol_values(workbench: StockWorkbench) -> list[tuple[str, str]]:
    insights = workbench.insights
    values = [
        ("feature_snapshot", workbench.feature_snapshot.symbol),
        ("factor_lab", workbench.factor_lab.symbol),
        ("market_regime", workbench.market_regime.symbol),
        ("signal_validation", workbench.signal_validation.symbol),
        ("risk_reward", workbench.risk_reward.symbol),
        ("timeframe_alignment", workbench.timeframe_alignment.symbol),
        ("alpha_evidence", workbench.alpha_evidence.symbol),
        ("diagnosis", workbench.diagnosis.symbol),
        ("evidence_chain", workbench.evidence_chain.symbol),
        ("qa_report", workbench.qa_report.symbol),
        ("event_digest", workbench.event_digest.symbol),
        ("peer_comparison", workbench.peer_comparison.symbol),
        ("t_strategy", workbench.t_strategy.symbol),
        ("risk_radar", workbench.risk_radar.symbol),
        ("chip_analysis", workbench.chip_analysis.symbol),
        ("leadership", workbench.leadership.symbol),
        ("theme_context", workbench.theme_context.symbol),
        ("replay", workbench.replay.symbol),
        ("chart_marks", workbench.chart_marks.symbol),
        ("insights.overview", insights.overview.symbol),
        ("insights.fund_flow", insights.fund_flow.symbol),
        ("insights.order_pressure", insights.order_pressure.symbol),
        ("insights.events", insights.events.symbol),
        ("insights.financial_health", insights.financial_health.symbol),
        ("insights.valuation", insights.valuation.symbol),
        ("insights.lhb", insights.lhb.symbol),
        ("insights.abnormal_events", insights.abnormal_events.symbol),
        ("insights.rule_matches", insights.rule_matches.symbol),
    ]
    if workbench.analysis.stock_profile is not None:
        values.append(("analysis.stock_profile", workbench.analysis.stock_profile.symbol))
    if workbench.analysis.review is not None:
        values.append(("analysis.review", workbench.analysis.review.symbol))
    values.extend(("alert_rule", item.symbol) for item in workbench.alert_rules)
    values.extend(("alert_event", item.symbol) for item in workbench.alert_events)
    values.extend(("note", item.symbol) for item in workbench.notes)
    values.extend(("insights.strategy_card", item.symbol) for item in insights.strategy_cards)
    return values


def _validate_workbench_metadata(workbench: StockWorkbench, symbol: str) -> None:
    generated = parse_audit_time(workbench.generated_at)
    context_generated = parse_audit_time(workbench.context_generated_at)
    future_limit = utc_now() + timedelta(minutes=5)
    if generated > future_limit or context_generated > future_limit:
        raise ValueError("workbench decision/generated time cannot be in the future")
    if generated < context_generated:
        raise ValueError("workbench response cannot predate its research context")
    if workbench.context_generated_at != workbench.research_cohort.decision_time:
        raise ValueError("workbench context timestamp mismatch")
    if workbench.research_mode != workbench.research_cohort.mode:
        raise ValueError("workbench research mode mismatch")
    if workbench.production_effect != workbench.research_cohort.production_effect:
        raise ValueError("workbench production effect mismatch")
    if symbol not in {
        workbench.research_cohort.requested_symbol,
        workbench.research_cohort.observed_symbol,
    }:
        raise ValueError("workbench cohort symbol mismatch")


def _validate_workbench_quote_binding(workbench: StockWorkbench, symbol: str) -> None:
    quote = workbench.analysis.quote
    observed = _canonical_a_share_symbol(f"{quote.code}.{quote.market}")
    if observed != symbol or quote.timestamp != workbench.research_cohort.quote_event_time:
        raise ValueError("workbench quote identity/timestamp mismatch")
    cutoff = _iso_date(workbench.research_cohort.daily_bar_cutoff, "daily_bar_cutoff")
    previous: date | None = None
    for index, row in enumerate(workbench.analysis.klines):
        current = _iso_date(row.date, f"analysis.klines[{index}].date")
        if current > cutoff:
            raise ValueError("workbench daily bar cannot follow cutoff")
        if previous is not None and current <= previous:
            raise ValueError("workbench daily bars must be strictly increasing")
        previous = current
    if previous is not None and previous != cutoff:
        raise ValueError("workbench daily bar cutoff mismatch")


def _validate_workbench_child_symbols(workbench: StockWorkbench, symbol: str) -> None:
    for label, value in _workbench_symbol_values(workbench):
        if _canonical_a_share_symbol(value) != symbol:
            raise ValueError(f"workbench {label} symbol mismatch")


def _validate_workbench_child_times(workbench: StockWorkbench) -> None:
    decision_time = parse_audit_time(workbench.research_cohort.decision_time)
    signal_date = _iso_date(workbench.research_cohort.signal_date, "signal_date")
    for label, child in _workbench_research_children(workbench):
        updated_at = parse_audit_time(child.updated_at)
        if updated_at > decision_time:
            raise ValueError(f"workbench {label} update cannot follow decision time")
        if updated_at.astimezone(ASHARE_TIMEZONE).date() != signal_date:
            raise ValueError(f"workbench {label} update must match signal date")


def _workbench_research_children(workbench: StockWorkbench) -> list[tuple[str, _UpdatedResearchChild]]:
    insights = workbench.insights
    return [
        ("feature_snapshot", workbench.feature_snapshot),
        ("factor_lab", workbench.factor_lab),
        ("market_regime", workbench.market_regime),
        ("signal_validation", workbench.signal_validation),
        ("risk_reward", workbench.risk_reward),
        ("timeframe_alignment", workbench.timeframe_alignment),
        ("alpha_evidence", workbench.alpha_evidence),
        ("diagnosis", workbench.diagnosis),
        ("evidence_chain", workbench.evidence_chain),
        ("qa_report", workbench.qa_report),
        ("event_digest", workbench.event_digest),
        ("peer_comparison", workbench.peer_comparison),
        ("t_strategy", workbench.t_strategy),
        ("risk_radar", workbench.risk_radar),
        ("chip_analysis", workbench.chip_analysis),
        ("leadership", workbench.leadership),
        ("theme_context", workbench.theme_context),
        ("replay", workbench.replay),
        ("insights.overview", insights.overview),
        ("insights.fund_flow", insights.fund_flow),
        ("insights.order_pressure", insights.order_pressure),
        ("insights.events", insights.events),
        ("insights.financial_health", insights.financial_health),
        ("insights.valuation", insights.valuation),
        ("insights.lhb", insights.lhb),
        ("insights.abnormal_events", insights.abnormal_events),
        ("insights.rule_matches", insights.rule_matches),
        *(("insights.strategy_card", item) for item in insights.strategy_cards),
    ]


def _canonical_a_share_symbol(value: str) -> str:
    normalized = standard_symbol(value)
    code, market = normalized.split(".", maxsplit=1)
    if not is_a_share_stock_code(code, market):
        raise ValueError("workbench symbol must be a valid A-share stock identity")
    return normalized


def _iso_date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"workbench {label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"workbench {label} must be an ISO date")
    return parsed


class StrongStockItem(BaseModel):
    rank: int
    code: str
    name: str
    price: float
    change_pct: float
    trend_score: int
    reason: str
    leader_score: int = 0
    tags: list[str] = Field(default_factory=list)


class QuoteSampleStatus(BaseModel):
    scope: str = "行情样本"
    requested_count: int = Field(default=0, ge=0)
    sample_count: int = Field(default=0, ge=0)
    missing_count: int = Field(default=0, ge=0)
    degraded: bool = False
    warnings: list[str] = Field(default_factory=list)


class StrongStockWatchResponse(BaseModel):
    updated_at: str
    items: list[StrongStockItem] = Field(default_factory=list)
    scope: str
    sample_count: int = Field(ge=0)
    requested_count: int = Field(default=0, ge=0)
    missing_count: int = Field(default=0, ge=0)
    degraded: bool = False
    warnings: list[str] = Field(default_factory=list)


class MarketOverview(BaseModel):
    indices: list[Quote]
    strong_stocks: list[StrongStockItem]
    risk_note: str
    index_meta: QuoteSampleStatus = Field(default_factory=lambda: QuoteSampleStatus(scope="市场指数样本"))
    strong_stocks_meta: QuoteSampleStatus = Field(default_factory=lambda: QuoteSampleStatus(scope="市场概览强股样本"))
    degraded: bool = False
    warnings: list[str] = Field(default_factory=list)
