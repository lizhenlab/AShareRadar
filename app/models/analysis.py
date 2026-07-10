"""Core stock analysis, strategy, finance, and insight models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.models.market import Kline, PlateItem, Quote, StockInfo


class SignalItem(BaseModel):
    title: str
    level: str = Field(description="积极、观察、谨慎、风险")
    reason: str


class KlineQuality(BaseModel):
    level: str
    source: str | None = None
    last_date: str | None = None
    latest_expected_date: str | None = None
    latest_allowed_date: str | None = None
    days_behind_expected: int | None = None
    from_cache: bool = False
    fallback_used: bool = False
    notes: list[str] = Field(default_factory=list)


class DataQuality(BaseModel):
    level: str
    source: str
    quote_time: str
    kline_count: int
    score: int = 100
    checked_at: str | None = None
    quote_delay_seconds: int | None = None
    consistency_level: str = "未校验"
    kline_quality: KlineQuality | None = None
    anomalies: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SignalContribution(BaseModel):
    category: str
    name: str
    impact: int
    level: str
    reason: str


class SignalSnapshot(BaseModel):
    score: int
    label: str
    confidence: int
    summary: str
    contributions: list[SignalContribution] = Field(default_factory=list)
    positive: list[SignalContribution] = Field(default_factory=list)
    negative: list[SignalContribution] = Field(default_factory=list)
    neutral: list[SignalContribution] = Field(default_factory=list)
    data_quality_notes: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


class FeatureSnapshot(BaseModel):
    symbol: str
    updated_at: str
    price: float
    change_pct: float
    trend_score: int
    trend_label: str
    signal_confidence: int
    data_quality_score: int
    data_quality_level: str
    leader_score: int
    leader_level: str
    support: float
    resistance: float
    ma5: float
    ma10: float
    ma20: float
    volume_ratio: float
    atr14: float = 0
    atr_pct: float = 0
    volatility_pct: float = 0
    turnover_rate: float | None = None
    amount: float | None = None
    valuation_score: int = 0
    financial_score: int = 0
    fund_flow_score: int = 0
    order_pressure: str = "--"
    industry_name: str | None = None
    industry_change_pct: float | None = None
    tags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ActionAdvice(BaseModel):
    action: str
    confidence: int
    reason: str


class ReviewPoint(BaseModel):
    label: str
    value: str
    level: str


class ReviewEvent(BaseModel):
    date: str
    title: str
    description: str
    level: str


class IndividualReview(BaseModel):
    symbol: str
    code: str
    market: str
    name: str
    period_days: int
    latest_close: float
    return_pct: float
    max_drawdown_pct: float
    volatility_pct: float
    positive_days: int
    negative_days: int
    trend_days: int
    review_label: str
    review_summary: str
    key_points: list[ReviewPoint]
    events: list[ReviewEvent]


class PeerSampleInfo(BaseModel):
    status: Literal["not_requested", "not_applicable", "insufficient", "available", "degraded", "unavailable"] = "not_requested"
    requested_count: int = Field(default=0, ge=0)
    missing_count: int = Field(default=0, ge=0)
    warning: str | None = None


class AnalysisResult(BaseModel):
    quote: Quote
    stock_profile: StockInfo | None = None
    industry_context: PlateItem | None = None
    action_advice: ActionAdvice
    data_quality: DataQuality
    signal_snapshot: SignalSnapshot
    review: IndividualReview | None = None
    trend_score: int
    trend_label: str
    support: float
    resistance: float
    ma5: float
    ma10: float
    ma20: float
    risk_level: str
    beginner_summary: str
    buy_points: list[SignalItem]
    sell_points: list[SignalItem]
    t_plan: list[SignalItem]
    strength_tags: list[str]
    klines: list[Kline]
    quote_history: list[dict[str, float | str | None]] = Field(default_factory=list)
    peer_quotes: list[Quote] = Field(default_factory=list)
    peer_sample: PeerSampleInfo = Field(default_factory=PeerSampleInfo)


class FactorScore(BaseModel):
    name: str
    score: int
    level: str
    summary: str
    evidence: list[str]
    missing_data: list[str] = Field(default_factory=list)


class KeyPriceLevel(BaseModel):
    label: str
    price: float
    note: str


class StockOverview(BaseModel):
    symbol: str
    code: str
    market: str
    name: str
    total_score: int
    total_level: str
    main_conflict: str
    beginner_takeaways: list[str]
    key_prices: list[KeyPriceLevel]
    risk_triggers: list[str]
    factors: list[FactorScore]
    action_advice: ActionAdvice
    updated_at: str


class FundFlowWindow(BaseModel):
    label: str
    score: int
    estimated_net_inflow: float | None = None
    summary: str


class FundFlowAnalysis(BaseModel):
    symbol: str
    available: bool
    source: str
    updated_at: str
    overall_score: int
    level: str
    estimated_main_net_inflow: float | None = None
    price_volume_relation: str
    windows: list[FundFlowWindow]
    notes: list[str]


class OrderPressure(BaseModel):
    symbol: str
    available: bool
    source: str
    updated_at: str
    pressure_level: str
    spread_pct: float | None = None
    bid_ask_ratio: float | None = None
    bid_amount: float | None = None
    ask_amount: float | None = None
    summary: str
    notes: list[str]


class StockEventItem(BaseModel):
    date: str
    title: str
    category: str
    level: str
    description: str
    source: str
    reliability: str = "推断"
    action_hint: str | None = None


class StockEventSummary(BaseModel):
    symbol: str
    updated_at: str
    events: list[StockEventItem]
    notes: list[str]
    missing_sources: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class StrategyCard(BaseModel):
    name: str
    status: str
    level: str
    trigger_conditions: list[str]
    current_evidence: list[str]
    reference_price: str
    invalidation: str
    suitable_for: str
    risk_note: str


class FinancialMetric(BaseModel):
    name: str
    value: str
    level: str
    summary: str
    source: str


class FinancialHealth(BaseModel):
    symbol: str
    updated_at: str
    score: int
    level: str
    summary: str
    metrics: list[FinancialMetric]
    highlights: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    source: str


class ValuationAnalysis(BaseModel):
    symbol: str
    updated_at: str
    score: int
    level: str
    summary: str
    pe: float | None = None
    pb: float | None = None
    market_cap: float | None = None
    market_cap_text: str | None = None
    price_percentile: float | None = None
    pe_percentile: float | None = None
    pb_percentile: float | None = None
    peer_pe_percentile: float | None = None
    peer_pb_percentile: float | None = None
    peer_sample_count: int = 0
    valuation_anchor_label: str = "历史锚待确认"
    evidence: list[str] = Field(default_factory=list)
    watch_points: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    source: str


class LhbSummary(BaseModel):
    symbol: str
    available: bool
    updated_at: str
    score: int
    level: str
    summary: str
    reasons: list[str] = Field(default_factory=list)
    seats: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    reliability: str = "前置推断"
    source: str


class AbnormalEventItem(BaseModel):
    date: str
    title: str
    level: str
    direction: str
    description: str
    evidence: list[str] = Field(default_factory=list)
    watch_points: list[str] = Field(default_factory=list)


class AbnormalEventSummary(BaseModel):
    symbol: str
    updated_at: str
    score: int
    level: str
    main_signal: str
    events: list[AbnormalEventItem]
    notes: list[str] = Field(default_factory=list)


class RuleDefinition(BaseModel):
    id: str
    name: str
    category: str
    description: str
    beginner_hint: str
    version: str = "rules.v1"
    parameters: dict[str, float | str] = Field(default_factory=dict)


class RuleMatch(BaseModel):
    rule_id: str
    name: str
    category: str
    status: str
    level: str
    confidence: int
    reason: str
    actions: list[str] = Field(default_factory=list)
    invalidation: str
    rule_version: str = "rules.v1"
    score_version: str = "score.v1"
    evidence: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)


class StockRuleMatchSummary(BaseModel):
    symbol: str
    updated_at: str
    matched_count: int
    top_level: str
    matches: list[RuleMatch]
    definitions: list[RuleDefinition] = Field(default_factory=list)


class StockInsightBundle(BaseModel):
    overview: StockOverview
    fund_flow: FundFlowAnalysis
    order_pressure: OrderPressure
    events: StockEventSummary
    strategy_cards: list[StrategyCard]
    financial_health: FinancialHealth
    valuation: ValuationAnalysis
    lhb: LhbSummary
    abnormal_events: AbnormalEventSummary
    rule_matches: StockRuleMatchSummary
