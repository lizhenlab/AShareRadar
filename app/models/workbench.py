"""Composite page-level models used by the stock workbench and market overview APIs."""

from __future__ import annotations

from typing import Literal

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


class WorkbenchDataWarning(BaseModel):
    component: Literal["advice_snapshot", "chart_marks", "alert_rules", "alert_events", "notes"]
    message: str


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
    local_data_warnings: list[WorkbenchDataWarning] = Field(default_factory=list)
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
