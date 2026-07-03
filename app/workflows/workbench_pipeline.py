from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import (
    AlphaEvidenceReport,
    AnalysisResult,
    ChipAnalysis,
    EventDigestReport,
    EvidenceChainReport,
    FactorLabReport,
    FeatureSnapshot,
    LeadershipReport,
    MarketRegimeReport,
    OrderBook,
    PeerComparisonReport,
    Quote,
    RiskRadarReport,
    RiskRewardReport,
    SignalValidationReport,
    StockConceptItem,
    StockDiagnosis,
    StockInsightBundle,
    StockQaReport,
    StockReplayAnalysis,
    TStrategyAssistantReport,
    ThemeContextReport,
    TimeframeAlignmentReport,
)
from app.services.datahub import DataHub
from app.services.market_sampling import market_breadth_quotes as _market_breadth_quotes
from app.services.provider_registry import provider_capability
from app.services.research import (
    build_alpha_evidence_report,
    build_chip_analysis,
    build_event_digest_report,
    build_evidence_chain_report,
    build_factor_lab_report,
    build_feature_snapshot,
    build_leadership_report,
    build_market_breadth_snapshot,
    build_market_regime_report,
    build_peer_comparison_report,
    build_replay_analysis,
    build_risk_radar_report,
    build_risk_reward_report,
    build_signal_validation_report,
    build_stock_diagnosis,
    build_stock_qa_report,
    build_t_strategy_assistant_report,
    build_theme_context_report,
    build_timeframe_alignment_report,
)
from app.services.research_breadth import MarketBreadthSnapshot
from app.services.stock_insights import build_stock_insight_bundle
from app.services.datahub_status import _provider_error_text
from app.services.workbench_context import WorkbenchContext
from app.workflows.stock_analysis import analyze_individual_stock


@dataclass(frozen=True)
class WorkbenchInputs:
    analysis: AnalysisResult
    breadth_quotes: list[Quote]
    order_book: OrderBook | None
    order_book_error: str | None
    concepts: list[StockConceptItem]


@dataclass(frozen=True)
class WorkbenchResearchCore:
    insights: StockInsightBundle
    feature_snapshot: FeatureSnapshot
    theme_context: ThemeContextReport
    chip_analysis: ChipAnalysis
    leadership: LeadershipReport
    factor_lab: FactorLabReport
    market_breadth: MarketBreadthSnapshot
    market_regime: MarketRegimeReport
    timeframe_alignment: TimeframeAlignmentReport
    signal_validation: SignalValidationReport
    risk_reward: RiskRewardReport


@dataclass(frozen=True)
class WorkbenchEvidence:
    alpha_evidence: AlphaEvidenceReport
    diagnosis: StockDiagnosis
    evidence_chain: EvidenceChainReport


@dataclass(frozen=True)
class WorkbenchSupportPanels:
    qa_report: StockQaReport
    event_digest: EventDigestReport
    peer_comparison: PeerComparisonReport
    t_strategy: TStrategyAssistantReport
    risk_radar: RiskRadarReport
    replay: StockReplayAnalysis


async def build_workbench_context(datahub: DataHub, symbol: str) -> WorkbenchContext:
    inputs = await _collect_workbench_inputs(datahub, symbol)
    core = _build_research_core(inputs)
    evidence = _build_evidence_chain(inputs.analysis, core)
    support_panels = _build_support_panels(inputs.analysis, core, evidence)
    return _workbench_context_from_parts(inputs, core, evidence, support_panels)


async def _collect_workbench_inputs(datahub: DataHub, symbol: str) -> WorkbenchInputs:
    analysis = await analyze_individual_stock(datahub, symbol, persist_history=False)
    breadth_quotes = await _market_breadth_quotes(datahub)
    order_book, order_book_error = await _order_book_or_error(datahub, symbol)
    concepts = await datahub.stock_concepts(symbol, limit=8)
    return WorkbenchInputs(
        analysis=analysis,
        breadth_quotes=breadth_quotes,
        order_book=order_book,
        order_book_error=order_book_error,
        concepts=concepts,
    )


def _build_research_core(inputs: WorkbenchInputs) -> WorkbenchResearchCore:
    analysis = inputs.analysis
    insights = build_stock_insight_bundle(analysis, order_book=inputs.order_book, order_book_error=inputs.order_book_error)
    feature_snapshot = build_feature_snapshot(analysis, insights)
    theme_context = build_theme_context_report(analysis, feature_snapshot, inputs.concepts)
    chip_analysis = build_chip_analysis(analysis, feature_snapshot)
    leadership = build_leadership_report(analysis, insights, feature_snapshot, inputs.concepts)
    factor_lab = build_factor_lab_report(analysis, insights, feature_snapshot, chip_analysis, leadership)
    market_breadth = build_market_breadth_snapshot(inputs.breadth_quotes)
    market_regime = build_market_regime_report(analysis, insights, feature_snapshot, factor_lab, market_breadth)
    timeframe_alignment = build_timeframe_alignment_report(analysis, feature_snapshot, factor_lab)
    signal_validation = build_signal_validation_report(analysis, feature_snapshot, factor_lab, market_regime, timeframe_alignment)
    risk_reward = build_risk_reward_report(analysis, feature_snapshot, factor_lab, market_regime, signal_validation, timeframe_alignment)
    return WorkbenchResearchCore(
        insights=insights,
        feature_snapshot=feature_snapshot,
        theme_context=theme_context,
        chip_analysis=chip_analysis,
        leadership=leadership,
        factor_lab=factor_lab,
        market_breadth=market_breadth,
        market_regime=market_regime,
        timeframe_alignment=timeframe_alignment,
        signal_validation=signal_validation,
        risk_reward=risk_reward,
    )


def _build_evidence_chain(analysis: AnalysisResult, core: WorkbenchResearchCore) -> WorkbenchEvidence:
    alpha_evidence = build_alpha_evidence_report(
        analysis,
        core.insights,
        core.feature_snapshot,
        core.factor_lab,
        core.market_regime,
        core.timeframe_alignment,
        core.risk_reward,
    )
    diagnosis = build_stock_diagnosis(
        analysis,
        core.insights,
        core.feature_snapshot,
        alpha_evidence,
        core.factor_lab,
        core.market_regime,
        core.signal_validation,
        core.risk_reward,
        core.timeframe_alignment,
    )
    return WorkbenchEvidence(
        alpha_evidence=alpha_evidence,
        diagnosis=diagnosis,
        evidence_chain=build_evidence_chain_report(diagnosis, alpha_evidence, core.signal_validation, core.risk_reward),
    )


def _build_support_panels(
    analysis: AnalysisResult,
    core: WorkbenchResearchCore,
    evidence: WorkbenchEvidence,
) -> WorkbenchSupportPanels:
    t_strategy = build_t_strategy_assistant_report(analysis, core.feature_snapshot, core.market_regime, core.signal_validation)
    return WorkbenchSupportPanels(
        qa_report=build_stock_qa_report(analysis, evidence.diagnosis, core.market_regime, core.risk_reward, t_strategy, core.theme_context),
        event_digest=build_event_digest_report(core.insights),
        peer_comparison=build_peer_comparison_report(analysis, core.insights, core.feature_snapshot),
        t_strategy=t_strategy,
        risk_radar=build_risk_radar_report(analysis, core.insights, core.feature_snapshot, core.market_regime, core.risk_reward, core.timeframe_alignment),
        replay=build_replay_analysis(analysis),
    )


def _workbench_context_from_parts(
    inputs: WorkbenchInputs,
    core: WorkbenchResearchCore,
    evidence: WorkbenchEvidence,
    support_panels: WorkbenchSupportPanels,
) -> WorkbenchContext:
    return WorkbenchContext(
        analysis=inputs.analysis,
        insights=core.insights,
        feature_snapshot=core.feature_snapshot,
        factor_lab=core.factor_lab,
        market_regime=core.market_regime,
        signal_validation=core.signal_validation,
        risk_reward=core.risk_reward,
        timeframe_alignment=core.timeframe_alignment,
        alpha_evidence=evidence.alpha_evidence,
        diagnosis=evidence.diagnosis,
        evidence_chain=evidence.evidence_chain,
        qa_report=support_panels.qa_report,
        event_digest=support_panels.event_digest,
        peer_comparison=support_panels.peer_comparison,
        t_strategy=support_panels.t_strategy,
        risk_radar=support_panels.risk_radar,
        chip_analysis=core.chip_analysis,
        leadership=core.leadership,
        theme_context=core.theme_context,
        replay=support_panels.replay,
        order_book_error=inputs.order_book_error,
    )


async def _order_book_or_error(datahub: DataHub, symbol: str) -> tuple[OrderBook | None, str | None]:
    order_book = None
    futu_provider = datahub.providers.get("futu")
    futu_capability = provider_capability(futu_provider) if futu_provider else None
    if not bool(futu_capability and futu_capability.enabled):
        return order_book, "Futu OpenAPI 未启用，盘口压力使用行情区间估算。"
    try:
        order_book = await datahub.order_book(symbol)
        return order_book, None
    except Exception as exc:
        return None, _provider_error_text(exc)


__all__ = ["build_workbench_context"]
