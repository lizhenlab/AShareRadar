from pydantic import BaseModel, Field


class Quote(BaseModel):
    code: str
    name: str
    market: str
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    volume: float
    amount: float
    change: float
    change_pct: float
    turnover_rate: float | None = None
    pe: float | None = None
    pb: float | None = None
    market_cap: float | None = None
    timestamp: str
    source: str = "腾讯行情"


class Kline(BaseModel):
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    source: str | None = None
    fetched_at: str | None = None
    from_cache: bool = False
    fallback_used: bool = False


class MinuteKline(BaseModel):
    timestamp: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float | None = None
    turnover_rate: float | None = None
    source: str | None = None
    interval: str = "5m"
    fetched_at: str | None = None
    from_cache: bool = False
    fallback_used: bool = False


class StockInfo(BaseModel):
    symbol: str
    code: str
    market: str
    name: str
    industry: str | None = None
    list_date: str | None = None
    source: str
    updated_at: str


class PlateItem(BaseModel):
    rank: int
    name: str
    change_pct: float
    amount: float | None = None
    turnover_rate: float | None = None
    leading_stock: str | None = None
    leading_stock_change_pct: float | None = None
    source: str
    updated_at: str


class StockConceptItem(BaseModel):
    symbol: str
    rank: int
    name: str
    change_pct: float = 0
    amount: float | None = None
    turnover_rate: float | None = None
    leading_stock: str | None = None
    leading_stock_change_pct: float | None = None
    match_reason: str = "概念成分匹配"
    source: str
    updated_at: str


class ProviderCapability(BaseModel):
    name: str
    installed: bool
    enabled: bool
    reliability_level: str = "公开源"
    realtime_quote: bool = False
    daily_kline: bool = False
    minute_kline: bool = False
    stock_pool: bool = False
    plate_rank: bool = False
    concept_board: bool = False
    order_book: bool = False
    note: str


class OrderBookLevel(BaseModel):
    price: float
    volume: float


class OrderBook(BaseModel):
    symbol: str
    code: str
    market: str
    bid: list[OrderBookLevel]
    ask: list[OrderBookLevel]
    source: str
    updated_at: str


class SignalItem(BaseModel):
    title: str
    level: str = Field(description="积极、观察、谨慎、风险")
    reason: str


class KlineQuality(BaseModel):
    level: str
    source: str | None = None
    last_date: str | None = None
    latest_expected_date: str | None = None
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


class AlphaEvidencePoint(BaseModel):
    source: str
    title: str
    impact: int
    level: str
    reason: str


class AlphaEvidenceReport(BaseModel):
    symbol: str
    updated_at: str
    confidence: int
    verdict: str
    summary: str
    positives: list[AlphaEvidencePoint] = Field(default_factory=list)
    negatives: list[AlphaEvidencePoint] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    data_quality_notes: list[str] = Field(default_factory=list)


class FactorCalibration(BaseModel):
    sample_count: int
    win_rate: float
    avg_forward_5d_return: float
    avg_forward_10d_return: float
    max_adverse_return: float
    stability_score: int = 0
    expected_level: str = "观察"
    confidence_level: str
    note: str


class CalibrationBucket(BaseModel):
    name: str
    sample_count: int
    win_rate: float
    avg_forward_5d_return: float
    avg_forward_10d_return: float
    note: str


class StandardFactor(BaseModel):
    id: str
    name: str
    category: str
    value: str
    score: int
    level: str
    direction: str
    percentile: float | None = None
    weight: float
    evidence: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    calibration: FactorCalibration | None = None
    calibration_buckets: list[CalibrationBucket] = Field(default_factory=list)


class FactorLabReport(BaseModel):
    symbol: str
    updated_at: str
    total_score: int
    calibrated_confidence: int
    calibration_sample_count: int = 0
    positive_factor_count: int = 0
    negative_factor_count: int = 0
    profile_label: str = "常规个股"
    weight_policy: list[str] = Field(default_factory=list)
    factors: list[StandardFactor]
    top_positive: list[str] = Field(default_factory=list)
    top_negative: list[str] = Field(default_factory=list)
    summary: str
    notes: list[str] = Field(default_factory=list)


class MarketRegimeReport(BaseModel):
    symbol: str
    updated_at: str
    market_label: str
    breadth_label: str = "市场宽度待确认"
    breadth_score: int = 50
    industry_label: str
    stock_state: str
    risk_multiplier: float
    confidence_adjustment: int
    suggestions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class SignalValidationItem(BaseModel):
    name: str
    category: str
    status: str
    confidence: int
    trigger_condition: str
    confirmation_condition: str
    invalidation_condition: str
    historical_reference: str
    action_hint: str


class SignalValidationReport(BaseModel):
    symbol: str
    updated_at: str
    overall_status: str
    summary: str
    items: list[SignalValidationItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ScenarioPlan(BaseModel):
    name: str
    probability: int
    trigger: str
    expected_move: str
    response: str
    invalidation: str


class RiskRewardReport(BaseModel):
    symbol: str
    updated_at: str
    current_price: float
    upside_target: float
    downside_stop: float
    upside_pct: float
    downside_pct: float
    reward_risk_ratio: float
    atr14: float = 0
    atr_pct: float = 0
    volatility_pct: float = 0
    rating: str
    summary: str
    scenarios: list[ScenarioPlan] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class TimeframeTrend(BaseModel):
    name: str
    window_days: int
    score: int
    label: str
    return_pct: float
    max_drawdown_pct: float
    above_ma: bool
    ma_value: float
    evidence: list[str] = Field(default_factory=list)


class TimeframeAlignmentReport(BaseModel):
    symbol: str
    updated_at: str
    alignment_score: int
    alignment_label: str
    conflict_level: str
    summary: str
    timeframes: list[TimeframeTrend] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


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


class StockDiagnosis(BaseModel):
    symbol: str
    updated_at: str
    headline: str
    beginner_summary: str
    professional_summary: str
    confirmation_signals: list[str] = Field(default_factory=list)
    hard_risks: list[str] = Field(default_factory=list)
    watch_focus: list[str] = Field(default_factory=list)
    action: str
    confidence: int


class EvidenceChainReport(BaseModel):
    verdict: str
    summary: str
    support: list[str] = Field(default_factory=list)
    opposition: list[str] = Field(default_factory=list)
    confirmations: list[str] = Field(default_factory=list)
    invalidations: list[str] = Field(default_factory=list)


class StockQaItem(BaseModel):
    question: str
    answer: str
    evidence: list[str] = Field(default_factory=list)


class StockQaReport(BaseModel):
    summary: str
    items: list[StockQaItem] = Field(default_factory=list)


class StockQuestionInput(BaseModel):
    symbol: str = Field(default="600519", min_length=1, max_length=20)
    question: str = Field(min_length=2, max_length=120)


class StockQuestionAnswer(BaseModel):
    symbol: str
    updated_at: str
    question: str
    topic: str
    conclusion: str
    answer: str
    confidence: int
    answer_source: str = "规则问诊"
    llm_used: bool = False
    llm_status: str | None = None
    evidence: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    invalidations: list[str] = Field(default_factory=list)
    related_questions: list[str] = Field(default_factory=list)


class EventDigestReport(BaseModel):
    impact_label: str
    summary: str
    positive_events: list[str] = Field(default_factory=list)
    negative_events: list[str] = Field(default_factory=list)
    watch_events: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)


class PeerComparisonReport(BaseModel):
    industry: str = "行业待确认"
    sample_count: int = 0
    valuation_position: str = "同行估值待确认"
    strength_position: str = "同行强弱待确认"
    summary: str
    metrics: list[str] = Field(default_factory=list)
    leaders: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class TStrategyAssistantReport(BaseModel):
    style: str
    suitability: str
    summary: str
    low_zone: str
    high_zone: str
    execution_steps: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)


class RiskRadarItem(BaseModel):
    name: str
    level: str
    score: int
    reason: str
    action: str


class RiskRadarReport(BaseModel):
    overall_level: str
    summary: str
    items: list[RiskRadarItem] = Field(default_factory=list)
    top_risks: list[str] = Field(default_factory=list)


class ChipBand(BaseModel):
    label: str
    low: float
    high: float
    share: float
    note: str


class ChipAnalysis(BaseModel):
    symbol: str
    updated_at: str
    center_price: float
    concentration: int
    distribution_label: str
    summary: str
    support_bands: list[ChipBand] = Field(default_factory=list)
    pressure_bands: list[ChipBand] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class LeadershipReport(BaseModel):
    symbol: str
    updated_at: str
    score: int
    level: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)


class ThemeContextReport(BaseModel):
    symbol: str
    updated_at: str
    industry: str = "行业待确认"
    industry_change_pct: float | None = None
    concepts: list[StockConceptItem] = Field(default_factory=list)
    score: int = 0
    level: str = "主题待确认"
    style: str = "背景不足"
    relative_strength: str = "强弱待确认"
    summary: str
    evidence: list[str] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)


class ReplayPatternStat(BaseModel):
    pattern: str
    sample_count: int
    win_rate: float
    avg_forward_5d_return: float
    note: str


class ReplayCase(BaseModel):
    date: str
    pattern: str
    entry_price: float
    forward_3d_return: float | None = None
    forward_5d_return: float | None = None
    forward_10d_return: float | None = None
    outcome: str
    note: str


class StockReplayAnalysis(BaseModel):
    symbol: str
    updated_at: str
    window_days: int
    sample_count: int
    success_rate: float
    summary: str
    pattern_stats: list[ReplayPatternStat] = Field(default_factory=list)
    cases: list[ReplayCase] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class MinuteSupportResistance(BaseModel):
    label: str
    price: float
    strength: int
    reason: str


class MinuteTPlan(BaseModel):
    low_zone: str
    high_zone: str
    suitability: str
    style: str
    confidence: int
    summary: str
    execution_steps: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)


class MinuteAnalysisReport(BaseModel):
    symbol: str
    updated_at: str
    interval: str
    source: str
    sample_count: int
    latest_price: float | None = None
    intraday_change_pct: float = 0
    intraday_range_pct: float = 0
    volume_pulse: str = "待确认"
    trend_label: str = "待确认"
    momentum_label: str = "待确认"
    summary: str
    supports: list[MinuteSupportResistance] = Field(default_factory=list)
    resistances: list[MinuteSupportResistance] = Field(default_factory=list)
    t_plan: MinuteTPlan
    warnings: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)


class AlertRuleInput(BaseModel):
    symbol: str
    condition_type: str
    threshold: float
    name: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=160)
    enabled: bool = True
    cooldown_seconds: int = Field(default=300, ge=30, le=86400)


class AlertRuleUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=40)
    threshold: float | None = None
    note: str | None = Field(default=None, max_length=160)
    enabled: bool | None = None
    cooldown_seconds: int | None = Field(default=None, ge=30, le=86400)


class AlertRuleItem(BaseModel):
    id: int
    symbol: str
    code: str
    market: str
    stock_name: str
    name: str
    condition_type: str
    condition_label: str
    threshold: float
    note: str | None = None
    enabled: bool = True
    last_checked_at: str | None = None
    last_triggered_at: str | None = None
    last_state: str = "等待"
    trigger_count: int = 0
    cooldown_seconds: int = 300
    created_at: str
    updated_at: str


class AlertEventItem(BaseModel):
    id: int
    rule_id: int
    symbol: str
    code: str
    market: str
    stock_name: str
    name: str
    condition_type: str
    event_type: str = "触发"
    message: str
    price: float
    change_pct: float
    threshold: float
    created_at: str


class AlertEvaluationItem(BaseModel):
    rule: AlertRuleItem
    current_value: float | None = None
    triggered: bool
    message: str
    event: AlertEventItem | None = None


class AlertEvaluationSummary(BaseModel):
    checked_at: str
    checked_count: int
    triggered_count: int
    new_event_count: int
    items: list[AlertEvaluationItem]


class StockNoteInput(BaseModel):
    symbol: str
    content: str = Field(min_length=1, max_length=500)
    note_type: str = Field(default="观察", max_length=20)
    price: float | None = None
    trade_date: str | None = Field(default=None, max_length=20)
    color: str | None = Field(default=None, max_length=20)
    visible: bool = True


class StockNoteUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=500)
    note_type: str | None = Field(default=None, max_length=20)
    price: float | None = None
    trade_date: str | None = Field(default=None, max_length=20)
    color: str | None = Field(default=None, max_length=20)
    visible: bool | None = None


class StockNoteItem(BaseModel):
    id: int
    symbol: str
    code: str
    market: str
    name: str
    note_type: str
    content: str
    price: float | None = None
    trade_date: str | None = None
    color: str | None = None
    visible: bool = True
    created_at: str
    updated_at: str


class ChartMarkItem(BaseModel):
    date: str
    kline_date: str | None = None
    price: float | None = None
    label: str
    category: str
    level: str
    description: str
    source: str
    color: str | None = None
    anchor_price_type: str = "close"
    visible: bool = True


class ChartMarkSummary(BaseModel):
    symbol: str
    updated_at: str
    marks: list[ChartMarkItem]
    categories: list[str] = Field(default_factory=list)


class StockWorkbench(BaseModel):
    symbol: str
    generated_at: str
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
    cache_policy: str = "同一只个股短时间内复用分析结果，避免重复请求外部行情源。"


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


class MarketOverview(BaseModel):
    indices: list[Quote]
    strong_stocks: list[StrongStockItem]
    risk_note: str


class ProviderStatus(BaseModel):
    name: str
    enabled: bool
    priority: int
    healthy: bool
    last_success: str | None = None
    last_error: str | None = None
    latency_ms: float | None = None
    success_count: int = 0
    failure_count: int = 0
    updated_at: str | None = None


class ProviderCapabilityStatus(BaseModel):
    name: str
    kind: str
    enabled: bool
    priority: int
    healthy: bool
    last_success: str | None = None
    last_error: str | None = None
    latency_ms: float | None = None
    success_count: int = 0
    failure_count: int = 0
    updated_at: str | None = None


class ProviderDecision(BaseModel):
    name: str
    role: str
    state: str
    priority: int
    capabilities: list[str] = Field(default_factory=list)
    success_rate: float | None = None
    last_success: str | None = None
    last_error: str | None = None
    action: str


class DataSourcePlan(BaseModel):
    primary_quote_source: str | None = None
    primary_kline_source: str | None = None
    primary_minute_source: str | None = None
    health_level: str
    summary: str
    decisions: list[ProviderDecision] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class CacheStats(BaseModel):
    path: str
    quote_count: int
    quote_history_count: int
    kline_count: int
    stock_count: int
    plate_count: int
    provider_count: int
    latest_quote_at: str | None = None
    latest_kline_at: str | None = None
    latest_stock_at: str | None = None
    latest_plate_at: str | None = None


class DataStatus(BaseModel):
    providers: list[ProviderStatus]
    cache: CacheStats
    capabilities: list[ProviderCapability] = Field(default_factory=list)
    capability_statuses: list[ProviderCapabilityStatus] = Field(default_factory=list)
    source_plan: DataSourcePlan | None = None


class WatchlistInput(BaseModel):
    symbol: str
    note: str | None = Field(default=None, max_length=80)
    group_name: str | None = Field(default=None, max_length=20)
    pinned: bool | None = None


class WatchlistItem(BaseModel):
    symbol: str
    code: str
    market: str
    name: str
    note: str | None = None
    group_name: str = "默认"
    pinned: bool = False
    latest_price: float | None = None
    latest_change_pct: float | None = None
    latest_source: str | None = None
    latest_at: str | None = None
    created_at: str
    updated_at: str


class AdviceHistoryItem(BaseModel):
    id: int
    symbol: str
    code: str
    market: str
    name: str
    action: str
    confidence: int
    trend_score: int
    trend_label: str
    risk_level: str
    price: float
    change_pct: float
    support: float
    resistance: float
    data_quality_score: int
    data_quality_level: str
    reason: str
    summary: str
    created_at: str
    updated_at: str | None = None
    repeat_count: int = 1


class TaskRun(BaseModel):
    id: int
    task_name: str
    status: str
    started_at: str
    finished_at: str | None = None
    duration_ms: int | None = None
    message: str | None = None


class MonitorEvent(BaseModel):
    id: int
    level: str
    category: str
    symbol: str | None = None
    message: str
    created_at: str
    last_seen_at: str | None = None
    repeat_count: int = 1


class ScheduledTaskState(BaseModel):
    name: str
    display_name: str
    interval_seconds: int
    running: bool
    last_started_at: str | None = None
    last_finished_at: str | None = None
    next_run_at: str | None = None
    last_status: str | None = None
    last_message: str | None = None


class SchedulerStatus(BaseModel):
    enabled: bool
    running: bool
    started_at: str | None = None
    task_count: int
    tasks: list[ScheduledTaskState]


class CacheFreshness(BaseModel):
    latest_quote_age_seconds: int | None = None
    latest_kline_age_seconds: int | None = None
    latest_stock_age_seconds: int | None = None
    latest_plate_age_seconds: int | None = None


class StorageDiagnostics(BaseModel):
    db_path: str
    db_size_bytes: int
    db_size_mb: float
    runtime_rows: int
    user_rows: int


class SystemDiagnostics(BaseModel):
    checked_at: str
    cache: CacheStats
    freshness: CacheFreshness
    storage: StorageDiagnostics
    scheduler: SchedulerStatus
    providers: list[ProviderStatus]
    table_counts: dict[str, int]
    warnings: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
