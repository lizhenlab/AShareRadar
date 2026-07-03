"""Composite page-level models used by the stock workbench and market overview APIs."""

from __future__ import annotations

from pydantic import BaseModel, Field

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
