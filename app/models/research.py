"""Research, diagnosis, question-answering, replay, and intraday report models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.analysis import PeerSampleInfo
from app.models.market import StockConceptItem


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
    sample_status: PeerSampleInfo = Field(default_factory=PeerSampleInfo)
    warnings: list[str] = Field(default_factory=list)


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
