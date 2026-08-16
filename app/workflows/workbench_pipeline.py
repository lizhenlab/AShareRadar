from __future__ import annotations

import asyncio
from dataclasses import dataclass

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
from app.models.analysis import (
    AnalysisResult,
    FeatureSnapshot,
    StockInsightBundle,
)
from app.models.market import (
    OrderBook,
    Quote,
    StockConceptItem,
)
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io_best_effort
from app.services.market_sampling import (
    MarketBreadthQuoteResult,
    QuoteSampleResult,
    market_breadth_quote_sample as _market_breadth_quote_sample,
)
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
from app.services.datahub_status import provider_error_text
from app.services.workbench_context import WorkbenchContext, workbench_cache_cohort_key
from app.utils.audit_time import audit_now_text
from app.utils.market_time import normalize_market_datetime
from app.utils.symbols import standard_symbol
from app.workflows.optional_data import optional_workflow_value, short_error
from app.workflows.stock_analysis import analyze_individual_stock


@dataclass(frozen=True)
class WorkbenchInputs:
    analysis: AnalysisResult
    context_generated_at: str
    breadth_quotes: list[Quote]
    breadth_warnings: tuple[str, ...]
    order_book: OrderBook | None
    order_book_error: str | None
    concepts: list[StockConceptItem]
    concept_error: str | None


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
    cache_cohort_key = workbench_cache_cohort_key()
    inputs = await _collect_workbench_inputs(datahub, symbol)
    core = _build_research_core(inputs)
    evidence = _build_evidence_chain(inputs.analysis, core)
    support_panels = _build_support_panels(inputs.analysis, core, evidence)
    return _workbench_context_from_parts(
        symbol,
        inputs,
        core,
        evidence,
        support_panels,
        cache_cohort_key=cache_cohort_key,
    )


async def _collect_workbench_inputs(datahub: DataHub, symbol: str) -> WorkbenchInputs:
    analysis = await analyze_individual_stock(datahub, symbol, persist_history=False)
    breadth_sample, order_book_result, concept_result = await asyncio.gather(
        _market_breadth_sample_or_empty(datahub),
        _order_book_or_error(datahub, symbol),
        _stock_concepts_or_error(datahub, symbol),
    )
    order_book, order_book_error = order_book_result
    concepts, concept_error = concept_result
    context_generated_at = audit_now_text()
    breadth_quotes, breadth_warnings = _time_aligned_breadth(
        analysis,
        list(breadth_sample.quotes),
        breadth_sample.warnings,
        decision_cutoff=context_generated_at,
    )
    order_book, order_book_error = _bound_order_book(
        analysis,
        order_book,
        order_book_error,
        decision_cutoff=context_generated_at,
    )
    concepts, concept_error = _bound_concepts(
        analysis,
        concepts,
        concept_error,
        decision_cutoff=context_generated_at,
    )
    return WorkbenchInputs(
        analysis=analysis,
        context_generated_at=context_generated_at,
        breadth_quotes=breadth_quotes,
        breadth_warnings=breadth_warnings,
        order_book=order_book,
        order_book_error=order_book_error,
        concepts=concepts,
        concept_error=concept_error,
    )


def _time_aligned_breadth(
    analysis: AnalysisResult,
    rows: list[Quote],
    warnings: tuple[str, ...],
    *,
    decision_cutoff: str | None = None,
) -> tuple[list[Quote], tuple[str, ...]]:
    signal_date = _quote_date(analysis.quote)
    cutoff = _component_cutoff(analysis, decision_cutoff)
    aligned = [
        row
        for row in rows
        if not row.fallback_used
        and _quote_date(row) == signal_date
        and _event_at_or_before(row.timestamp, cutoff)
    ]
    if len(aligned) == len(rows):
        return aligned, warnings
    warning = "市场宽度含不同交易日或降级行情，已从本次环境评分剔除。"
    return aligned, tuple(dict.fromkeys((*warnings, warning)))


def _bound_order_book(
    analysis: AnalysisResult,
    order_book: OrderBook | None,
    error: str | None,
    *,
    decision_cutoff: str | None = None,
) -> tuple[OrderBook | None, str | None]:
    if order_book is None:
        return None, error
    expected = standard_symbol(f"{analysis.quote.code}.{analysis.quote.market}")
    updated = normalize_market_datetime(order_book.updated_at)
    if (
        _same_symbol(order_book.symbol, expected)
        and updated is not None
        and updated[:10] == _quote_date(analysis.quote)
        and updated <= _component_cutoff(analysis, decision_cutoff)
    ):
        return order_book, error
    return None, "盘口股票身份或交易日与当前个股研究不一致，已按不可用处理。"


def _bound_concepts(
    analysis: AnalysisResult,
    rows: list[StockConceptItem],
    error: str | None,
    *,
    decision_cutoff: str | None = None,
) -> tuple[list[StockConceptItem], str | None]:
    expected = standard_symbol(f"{analysis.quote.code}.{analysis.quote.market}")
    signal_date = _quote_date(analysis.quote)
    cutoff = _component_cutoff(analysis, decision_cutoff)
    aligned = [
        row
        for row in rows
        if not row.fallback_used
        and _same_symbol(row.symbol, expected)
        and (updated := normalize_market_datetime(row.updated_at)) is not None
        and updated[:10] == signal_date
        and updated <= cutoff
    ]
    if len(aligned) == len(rows):
        return aligned, error
    return aligned, "概念证据含不同股票或交易日，已从本次主题评分剔除。"


def _quote_date(quote: Quote) -> str:
    value = normalize_market_datetime(quote.timestamp)
    if value is None:
        raise ValueError("个股工作台行情时间无效")
    return value[:10]


def _decision_cutoff(value: str | None) -> str:
    cutoff = normalize_market_datetime(value or audit_now_text())
    if cutoff is None:
        raise ValueError("个股工作台决策截止时间无效")
    return cutoff


def _component_cutoff(analysis: AnalysisResult, decision_cutoff: str | None) -> str:
    """Bind optional inputs to the primary quote, never a later same-day state."""

    quote_cutoff = normalize_market_datetime(analysis.quote.timestamp)
    if quote_cutoff is None:
        raise ValueError("个股工作台行情截止时间无效")
    return min(quote_cutoff, _decision_cutoff(decision_cutoff))


def _event_at_or_before(value: object, cutoff: str) -> bool:
    observed = normalize_market_datetime(value)
    return observed is not None and observed <= cutoff


def _same_symbol(value: object, expected: str) -> bool:
    try:
        return standard_symbol(str(value)) == expected
    except (TypeError, ValueError):
        return False


async def _market_breadth_sample_or_empty(datahub: DataHub) -> MarketBreadthQuoteResult:
    failure: Exception | None = None

    def unavailable_sample(exc: Exception) -> MarketBreadthQuoteResult:
        nonlocal failure
        failure = exc
        return _unavailable_market_breadth_sample()

    sample = await optional_workflow_value(
        datahub,
        lambda: _market_breadth_quote_sample(datahub),
        unavailable_sample,
    )
    if failure is not None:
        message = "市场宽度数据源请求失败，环境判断已降级。"
        log_event = getattr(datahub.cache, "log_event", None)
        if callable(log_event):
            await run_cache_io_best_effort(log_event, "fallback", f"{message}；{short_error(failure)}")
    return sample


def _build_research_core(inputs: WorkbenchInputs) -> WorkbenchResearchCore:
    analysis = inputs.analysis
    insights = build_stock_insight_bundle(analysis, order_book=inputs.order_book, order_book_error=inputs.order_book_error)
    feature_snapshot = build_feature_snapshot(analysis, insights)
    theme_context = build_theme_context_report(analysis, feature_snapshot, inputs.concepts, concept_error=inputs.concept_error)
    chip_analysis = build_chip_analysis(analysis, feature_snapshot)
    leadership = build_leadership_report(analysis, insights, feature_snapshot, inputs.concepts, concept_error=inputs.concept_error)
    factor_lab = build_factor_lab_report(analysis, insights, feature_snapshot, chip_analysis, leadership)
    market_breadth = build_market_breadth_snapshot(inputs.breadth_quotes, warnings=inputs.breadth_warnings)
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
        evidence_chain=build_evidence_chain_report(
            analysis,
            diagnosis,
            alpha_evidence,
            core.signal_validation,
            core.risk_reward,
        ),
    )


def _build_support_panels(
    analysis: AnalysisResult,
    core: WorkbenchResearchCore,
    evidence: WorkbenchEvidence,
) -> WorkbenchSupportPanels:
    t_strategy = build_t_strategy_assistant_report(analysis, core.feature_snapshot, core.market_regime, core.signal_validation)
    return WorkbenchSupportPanels(
        qa_report=build_stock_qa_report(analysis, evidence.diagnosis, core.market_regime, core.risk_reward, t_strategy, core.theme_context),
        event_digest=build_event_digest_report(analysis, core.insights),
        peer_comparison=build_peer_comparison_report(analysis, core.insights, core.feature_snapshot),
        t_strategy=t_strategy,
        risk_radar=build_risk_radar_report(analysis, core.insights, core.feature_snapshot, core.market_regime, core.risk_reward, core.timeframe_alignment),
        replay=build_replay_analysis(analysis),
    )


def _workbench_context_from_parts(
    requested_symbol: str,
    inputs: WorkbenchInputs,
    core: WorkbenchResearchCore,
    evidence: WorkbenchEvidence,
    support_panels: WorkbenchSupportPanels,
    *,
    cache_cohort_key: str,
) -> WorkbenchContext:
    requested = standard_symbol(requested_symbol)
    observed = standard_symbol(f"{inputs.analysis.quote.code}.{inputs.analysis.quote.market}")
    quote_event_time = normalize_market_datetime(inputs.analysis.quote.timestamp)
    if quote_event_time is None:
        raise ValueError("个股工作台 quote event time 无效")
    daily_bar_cutoff = inputs.analysis.klines[-1].date if inputs.analysis.klines else ""
    if not daily_bar_cutoff:
        raise ValueError("个股工作台缺少已完成日K截止日")
    context_generated_at = inputs.context_generated_at
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
        requested_symbol=requested,
        observed_symbol=observed,
        context_generated_at=context_generated_at,
        signal_date=quote_event_time[:10],
        daily_bar_cutoff=daily_bar_cutoff,
        quote_event_time=inputs.analysis.quote.timestamp,
        cache_cohort_key=cache_cohort_key,
    )


async def _order_book_or_error(datahub: DataHub, symbol: str) -> tuple[OrderBook | None, str | None]:
    try:
        futu_provider = datahub.providers.get("futu")
        futu_capability = provider_capability(futu_provider) if futu_provider else None
        if not bool(futu_capability and futu_capability.enabled and futu_capability.order_book):
            return None, "Futu OpenAPI 未启用，盘口压力使用行情区间估算。"
        return await optional_workflow_value(
            datahub,
            lambda: _load_order_book(datahub, symbol),
            lambda exc: (None, provider_error_text(exc)),
        )
    except Exception as exc:
        return None, provider_error_text(exc)


async def _stock_concepts_or_error(datahub: DataHub, symbol: str) -> tuple[list[StockConceptItem], str | None]:
    return await optional_workflow_value(
        datahub,
        lambda: _load_stock_concepts(datahub, symbol),
        lambda exc: ([], provider_error_text(exc)),
    )


async def _load_order_book(datahub: DataHub, symbol: str) -> tuple[OrderBook | None, str | None]:
    return await datahub.order_book(symbol), None


async def _load_stock_concepts(datahub: DataHub, symbol: str) -> tuple[list[StockConceptItem], str | None]:
    result = await datahub.cached_stock_concepts_result(symbol, limit=8)
    if result.used_fallback_cache:
        return [], "概念数据源不可用，过期缓存不参与主题与龙头强度评分。"
    return result.rows, None


def _unavailable_market_breadth_sample() -> MarketBreadthQuoteResult:
    message = "市场宽度数据源请求失败，环境判断已降级。"
    return MarketBreadthQuoteResult(
        quote_sample=QuoteSampleResult(requested_symbols=(), quotes=(), missing_symbols=()),
        warnings=(message,),
    )


__all__ = ["build_workbench_context"]
