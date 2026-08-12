from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from app.models.analysis import (
    AbnormalEventSummary,
    FactorScore,
    FeatureSnapshot,
    FinancialHealth,
    FundFlowAnalysis,
    LhbSummary,
    OrderPressure,
    RuleDefinition,
    StockEventSummary,
    StockInsightBundle,
    StockOverview,
    StockRuleMatchSummary,
    StrategyCard,
    ValuationAnalysis,
)
from app.models.reviews import ResearchQueueRefreshSummary
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
    StockDiagnosis,
    StockQaReport,
    StockQuestionAnswer,
    StockQuestionInput,
    StockReplayAnalysis,
    TStrategyAssistantReport,
    ThemeContextReport,
)
from app.models.user_data import (
    AlertEventItem,
    AlertRuleItem,
    ChartMarkSummary,
    StockNoteItem,
)
from app.models.workbench import (
    StockWorkbench,
    WorkbenchDataWarning,
)
from app.services import chart_marks as chart_marks_service
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io, run_cache_io_best_effort
from app.services.data_quality_time import quote_event_time_error
from app.services.llm_explainer import enhance_stock_answer
from app.services.research import answer_stock_question
from app.services.trading_calendar import is_trading_day
from app.services.stock_insights import rule_definitions
from app.services.workbench_context import WorkbenchContext, WorkbenchContextCache
from app.utils.audit_time import audit_now_text as now_text
from app.utils.clock import performance_now
from app.utils.symbols import standard_symbol
from app.workflows import active_research_queue as _active_research_queue
from app.workflows.market_overview import market_overview, strong_stock_watch
from app.workflows.stock_analysis import analyze_individual_stock, review_individual_stock, stock_minute_analysis
from app.workflows.stock_lookup import confirmed_stock_profile as _confirmed_stock_profile
from app.workflows.stock_lookup import match_industry
from app.workflows.workbench_pipeline import build_workbench_context as _build_workbench_context


WORKBENCH_CHART_MARK_LIMIT = 80
WORKBENCH_ALERT_RULE_LIMIT = 100
WORKBENCH_ALERT_EVENT_LIMIT = 20
WORKBENCH_NOTE_LIMIT = 50
ACTIVE_RESEARCH_REFRESH_LIMIT = _active_research_queue.ACTIVE_RESEARCH_REFRESH_LIMIT
MAX_ACTIVE_RESEARCH_REFRESH_LIMIT = _active_research_queue.MAX_ACTIVE_RESEARCH_REFRESH_LIMIT
MIN_ACTIVE_RESEARCH_QUALITY_SCORE = _active_research_queue.MIN_ACTIVE_RESEARCH_QUALITY_SCORE
INVALID_RESEARCH_CONTRACT_VERSIONS = _active_research_queue.INVALID_RESEARCH_CONTRACT_VERSIONS
ACTIVE_RESEARCH_CURSOR_ATTR = _active_research_queue.ACTIVE_RESEARCH_CURSOR_ATTR


@dataclass(frozen=True)
class StockWorkbenchLocalState:
    chart_marks: ChartMarkSummary
    alert_rules: list[AlertRuleItem]
    alert_events: list[AlertEventItem]
    notes: list[StockNoteItem]
    warnings: list[WorkbenchDataWarning]


_active_research_candidate_order = _active_research_queue.active_research_candidate_order
_active_research_cursor = _active_research_queue.active_research_cursor
_active_research_refresh_window = _active_research_queue.active_research_refresh_window
_active_research_snapshot_rejection = _active_research_queue.active_research_snapshot_rejection
_advice_snapshot_contract_is_current = _active_research_queue.advice_snapshot_contract_is_current
_advice_snapshot_is_current = _active_research_queue.advice_snapshot_is_current
_advance_active_research_cursor = _active_research_queue.advance_active_research_cursor
_analysis_conclusion_values = _active_research_queue.analysis_conclusion_values
_analysis_data_date = _active_research_queue.analysis_data_date
_background_sensitive_values = _active_research_queue.background_sensitive_values
_latest_research_kline = _active_research_queue.latest_research_kline
_refresh_active_research_symbol = _active_research_queue.refresh_active_research_symbol
_same_research_price = _active_research_queue.same_research_price
_short_research_refresh_error = _active_research_queue.short_research_refresh_error


async def refresh_active_research_queue(
    datahub: DataHub,
    *,
    now: datetime | None = None,
    limit: int = ACTIVE_RESEARCH_REFRESH_LIMIT,
) -> ResearchQueueRefreshSummary:
    return await _active_research_queue.refresh_active_research_queue(
        datahub,
        now=now,
        limit=limit,
        analyzer=analyze_individual_stock,
        trading_day_check=is_trading_day,
    )


async def stock_workbench_context(
    datahub: DataHub,
    symbol: str,
    *,
    use_cache: bool = True,
    context_cache: WorkbenchContextCache | None = None,
) -> WorkbenchContext:
    cache = context_cache or datahub.workbench_contexts
    return await cache.get(symbol, lambda normalized: _build_workbench_context(datahub, normalized), use_cache=use_cache)


async def stock_insight_bundle(datahub: DataHub, symbol: str) -> StockInsightBundle:
    return (await stock_workbench_context(datahub, symbol)).insights


async def stock_workbench(datahub: DataHub, symbol: str) -> StockWorkbench:
    started = performance_now()
    try:
        context = await stock_workbench_context(datahub, symbol)
        normalized = _workbench_symbol(context.insights.overview.symbol)
        advice_warning = await _ensure_advice_snapshot(datahub, context)
        local_state = await _workbench_local_state(datahub, normalized, context)
        warnings = [item for item in [advice_warning, *local_state.warnings] if item is not None]
        result = _stock_workbench_response(context, normalized, local_state, warnings)
    except asyncio.CancelledError:
        raise
    except Exception:
        await _record_workbench_reliability(
            datahub,
            usable=False,
            duration_ms=_elapsed_ms(started),
        )
        raise
    await _record_workbench_reliability(
        datahub,
        usable=True,
        duration_ms=_elapsed_ms(started),
        quality=result.analysis.data_quality.score >= 50,
        fresh=_workbench_is_fresh(result),
        non_fallback=_workbench_is_non_fallback(result),
    )
    return result


async def _record_workbench_reliability(
    datahub: DataHub,
    *,
    usable: bool,
    duration_ms: int,
    quality: bool | None = None,
    fresh: bool | None = None,
    non_fallback: bool | None = None,
) -> None:
    recorder = getattr(datahub.cache, "record_workbench_reliability", None)
    if callable(recorder):
        await run_cache_io_best_effort(
            recorder,
            usable=usable,
            duration_ms=duration_ms,
            quality=quality,
            fresh=fresh,
            non_fallback=non_fallback,
        )


def _workbench_is_fresh(result: StockWorkbench) -> bool:
    quality = result.analysis.data_quality
    kline_quality = quality.kline_quality
    return (
        quote_event_time_error(result.analysis.quote.timestamp) is None
        and kline_quality is not None
        and kline_quality.days_behind_expected == 0
    )


def _workbench_is_non_fallback(result: StockWorkbench) -> bool:
    quote = result.analysis.quote
    kline_quality = result.analysis.data_quality.kline_quality
    if kline_quality is None:
        return False
    return not any(
        (
            quote.from_cache,
            quote.fallback_used,
            kline_quality.from_cache,
            kline_quality.fallback_used,
        )
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((performance_now() - started) * 1000))


async def _ensure_advice_snapshot(datahub: DataHub, context: WorkbenchContext) -> WorkbenchDataWarning | None:
    if context.advice_snapshot_saved:
        return None
    try:
        await run_cache_io(datahub.cache.save_advice_snapshot, context.analysis)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = "分析建议快照暂未保存，本次分析结果仍可正常查看。"
        await _log_local_state_failure(datahub, message, exc)
        return WorkbenchDataWarning(component="advice_snapshot", message=message)
    else:
        context.advice_snapshot_saved = True
        return None


async def _workbench_local_state(datahub: DataHub, normalized: str, context: WorkbenchContext) -> StockWorkbenchLocalState:
    normalized = _workbench_symbol(normalized)
    chart_marks, chart_warning = await _safe_chart_marks(datahub, normalized, context)
    alert_rules, rules_warning = await _safe_alert_rules(datahub, normalized)
    alert_events, events_warning = await _safe_alert_events(datahub, normalized)
    notes, notes_warning = await _safe_stock_notes(datahub, normalized)
    return StockWorkbenchLocalState(
        chart_marks=chart_marks,
        alert_rules=alert_rules,
        alert_events=alert_events,
        notes=notes,
        warnings=[item for item in [chart_warning, rules_warning, events_warning, notes_warning] if item is not None],
    )


def _workbench_symbol(symbol: str) -> str:
    return standard_symbol(symbol)


async def _safe_chart_marks(
    datahub: DataHub,
    normalized: str,
    context: WorkbenchContext,
) -> tuple[ChartMarkSummary, WorkbenchDataWarning | None]:
    try:
        marks = await chart_marks_service.build_chart_marks_from_context(
            datahub,
            normalized,
            context.insights,
            limit=WORKBENCH_CHART_MARK_LIMIT,
        )
        return marks, None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = "图表标注暂不可用，当前显示空标注。"
        await _log_local_state_failure(datahub, f"{message} 股票：{normalized}", exc)
        return (
            ChartMarkSummary(symbol=normalized, updated_at=now_text(), marks=[]),
            WorkbenchDataWarning(component="chart_marks", message=message),
        )


async def _safe_alert_rules(datahub: DataHub, normalized: str) -> tuple[list[AlertRuleItem], WorkbenchDataWarning | None]:
    try:
        rows = await run_cache_io(
            datahub.cache.alert_rules,
            symbol=normalized,
            include_disabled=True,
            limit=WORKBENCH_ALERT_RULE_LIMIT,
        )
        return rows, None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = "预警规则暂不可用，当前显示空列表。"
        await _log_local_state_failure(datahub, f"{message} 股票：{normalized}", exc)
        return [], WorkbenchDataWarning(component="alert_rules", message=message)


async def _safe_alert_events(datahub: DataHub, normalized: str) -> tuple[list[AlertEventItem], WorkbenchDataWarning | None]:
    try:
        rows = await run_cache_io(datahub.cache.alert_events, symbol=normalized, limit=WORKBENCH_ALERT_EVENT_LIMIT)
        return rows, None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = "预警事件暂不可用，当前显示空列表。"
        await _log_local_state_failure(datahub, f"{message} 股票：{normalized}", exc)
        return [], WorkbenchDataWarning(component="alert_events", message=message)


async def _safe_stock_notes(datahub: DataHub, normalized: str) -> tuple[list[StockNoteItem], WorkbenchDataWarning | None]:
    try:
        rows = await run_cache_io(datahub.cache.stock_notes, normalized, limit=WORKBENCH_NOTE_LIMIT)
        return rows, None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = "个股笔记暂不可用，当前显示空列表。"
        await _log_local_state_failure(datahub, f"{message} 股票：{normalized}", exc)
        return [], WorkbenchDataWarning(component="notes", message=message)


async def _log_local_state_failure(datahub: DataHub, message: str, exc: Exception) -> None:
    log_event = getattr(datahub.cache, "log_event", None)
    if callable(log_event):
        await run_cache_io_best_effort(log_event, "fallback", f"{message}；{exc.__class__.__name__}")


def _stock_workbench_response(
    context: WorkbenchContext,
    normalized: str,
    local_state: StockWorkbenchLocalState,
    warnings: list[WorkbenchDataWarning],
) -> StockWorkbench:
    return StockWorkbench(
        symbol=normalized,
        generated_at=now_text(),
        analysis=context.analysis,
        insights=context.insights,
        feature_snapshot=context.feature_snapshot,
        factor_lab=context.factor_lab,
        market_regime=context.market_regime,
        signal_validation=context.signal_validation,
        risk_reward=context.risk_reward,
        timeframe_alignment=context.timeframe_alignment,
        alpha_evidence=context.alpha_evidence,
        diagnosis=context.diagnosis,
        evidence_chain=context.evidence_chain,
        qa_report=context.qa_report,
        event_digest=context.event_digest,
        peer_comparison=context.peer_comparison,
        t_strategy=context.t_strategy,
        risk_radar=context.risk_radar,
        chip_analysis=context.chip_analysis,
        leadership=context.leadership,
        theme_context=context.theme_context,
        replay=context.replay,
        chart_marks=local_state.chart_marks,
        alert_rules=local_state.alert_rules,
        alert_events=local_state.alert_events,
        notes=local_state.notes,
        local_data_warnings=warnings[:5],
    )


async def stock_feature_snapshot(datahub: DataHub, symbol: str) -> FeatureSnapshot:
    return (await stock_workbench_context(datahub, symbol)).feature_snapshot


async def stock_factor_lab(datahub: DataHub, symbol: str) -> FactorLabReport:
    return (await stock_workbench_context(datahub, symbol)).factor_lab


async def stock_market_regime(datahub: DataHub, symbol: str) -> MarketRegimeReport:
    return (await stock_workbench_context(datahub, symbol)).market_regime


async def stock_alpha_evidence(datahub: DataHub, symbol: str) -> AlphaEvidenceReport:
    return (await stock_workbench_context(datahub, symbol)).alpha_evidence


async def stock_diagnosis(datahub: DataHub, symbol: str) -> StockDiagnosis:
    return (await stock_workbench_context(datahub, symbol)).diagnosis


async def stock_evidence_chain(datahub: DataHub, symbol: str) -> EvidenceChainReport:
    return (await stock_workbench_context(datahub, symbol)).evidence_chain


async def stock_qa_report(datahub: DataHub, symbol: str) -> StockQaReport:
    return (await stock_workbench_context(datahub, symbol)).qa_report


async def stock_event_digest(datahub: DataHub, symbol: str) -> EventDigestReport:
    return (await stock_workbench_context(datahub, symbol)).event_digest


async def stock_peer_comparison(datahub: DataHub, symbol: str) -> PeerComparisonReport:
    return (await stock_workbench_context(datahub, symbol)).peer_comparison


async def stock_t_strategy(datahub: DataHub, symbol: str) -> TStrategyAssistantReport:
    return (await stock_workbench_context(datahub, symbol)).t_strategy


async def stock_risk_radar(datahub: DataHub, symbol: str) -> RiskRadarReport:
    return (await stock_workbench_context(datahub, symbol)).risk_radar


async def stock_question_answer(datahub: DataHub, payload: StockQuestionInput) -> StockQuestionAnswer:
    context = await stock_workbench_context(datahub, payload.symbol)
    rule_answer = answer_stock_question(
        payload.question,
        context.analysis,
        context.diagnosis,
        context.evidence_chain,
        context.risk_radar,
        context.event_digest,
        context.peer_comparison,
        context.t_strategy,
        context.market_regime,
        context.risk_reward,
        context.signal_validation,
        context.timeframe_alignment,
        context.theme_context,
    )
    return await enhance_stock_answer(settings=datahub.settings, rule_answer=rule_answer, analysis=context.analysis)


async def stock_chip_analysis(datahub: DataHub, symbol: str) -> ChipAnalysis:
    return (await stock_workbench_context(datahub, symbol)).chip_analysis


async def stock_leadership(datahub: DataHub, symbol: str) -> LeadershipReport:
    return (await stock_workbench_context(datahub, symbol)).leadership


async def stock_theme_context(datahub: DataHub, symbol: str) -> ThemeContextReport:
    return (await stock_workbench_context(datahub, symbol)).theme_context


async def stock_replay(datahub: DataHub, symbol: str) -> StockReplayAnalysis:
    return (await stock_workbench_context(datahub, symbol)).replay


async def stock_overview(datahub: DataHub, symbol: str) -> StockOverview:
    return (await stock_insight_bundle(datahub, symbol)).overview


async def stock_factors(datahub: DataHub, symbol: str) -> list[FactorScore]:
    return (await stock_insight_bundle(datahub, symbol)).overview.factors


async def stock_fund_flow(datahub: DataHub, symbol: str) -> FundFlowAnalysis:
    return (await stock_insight_bundle(datahub, symbol)).fund_flow


async def stock_order_pressure(datahub: DataHub, symbol: str) -> OrderPressure:
    return (await stock_insight_bundle(datahub, symbol)).order_pressure


async def stock_events(datahub: DataHub, symbol: str) -> StockEventSummary:
    return (await stock_insight_bundle(datahub, symbol)).events


async def stock_strategy_cards(datahub: DataHub, symbol: str) -> list[StrategyCard]:
    return (await stock_insight_bundle(datahub, symbol)).strategy_cards


async def stock_financial_health(datahub: DataHub, symbol: str) -> FinancialHealth:
    return (await stock_insight_bundle(datahub, symbol)).financial_health


async def stock_valuation(datahub: DataHub, symbol: str) -> ValuationAnalysis:
    return (await stock_insight_bundle(datahub, symbol)).valuation


async def stock_lhb(datahub: DataHub, symbol: str) -> LhbSummary:
    return (await stock_insight_bundle(datahub, symbol)).lhb


async def stock_abnormal_events(datahub: DataHub, symbol: str) -> AbnormalEventSummary:
    return (await stock_insight_bundle(datahub, symbol)).abnormal_events


async def stock_rule_matches(datahub: DataHub, symbol: str) -> StockRuleMatchSummary:
    return (await stock_insight_bundle(datahub, symbol)).rule_matches


def stock_rule_definitions() -> list[RuleDefinition]:
    return rule_definitions()


__all__ = [
    "_build_workbench_context",
    "_confirmed_stock_profile",
    "analyze_individual_stock",
    "market_overview",
    "match_industry",
    "refresh_active_research_queue",
    "review_individual_stock",
    "stock_abnormal_events",
    "stock_alpha_evidence",
    "stock_chip_analysis",
    "stock_diagnosis",
    "stock_event_digest",
    "stock_events",
    "stock_evidence_chain",
    "stock_factor_lab",
    "stock_factors",
    "stock_feature_snapshot",
    "stock_financial_health",
    "stock_fund_flow",
    "stock_insight_bundle",
    "stock_leadership",
    "stock_lhb",
    "stock_market_regime",
    "stock_minute_analysis",
    "stock_order_pressure",
    "stock_overview",
    "stock_peer_comparison",
    "stock_qa_report",
    "stock_question_answer",
    "stock_replay",
    "stock_risk_radar",
    "stock_rule_definitions",
    "stock_rule_matches",
    "stock_strategy_cards",
    "stock_t_strategy",
    "stock_theme_context",
    "stock_valuation",
    "stock_workbench",
    "stock_workbench_context",
    "strong_stock_watch",
]
