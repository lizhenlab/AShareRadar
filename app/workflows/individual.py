from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.models.schemas import (
    AbnormalEventSummary,
    AlphaEvidenceReport,
    AlertEventItem,
    AlertRuleItem,
    ChartMarkSummary,
    ChipAnalysis,
    EventDigestReport,
    EvidenceChainReport,
    FactorLabReport,
    FactorScore,
    FeatureSnapshot,
    FinancialHealth,
    FundFlowAnalysis,
    LeadershipReport,
    LhbSummary,
    MarketRegimeReport,
    OrderPressure,
    PeerComparisonReport,
    RiskRadarReport,
    RuleDefinition,
    StockDiagnosis,
    StockEventSummary,
    StockInsightBundle,
    StockOverview,
    StockQaReport,
    StockQuestionAnswer,
    StockQuestionInput,
    StockReplayAnalysis,
    StockRuleMatchSummary,
    StockNoteItem,
    StockWorkbench,
    StrategyCard,
    TStrategyAssistantReport,
    ThemeContextReport,
    ValuationAnalysis,
    WorkbenchDataWarning,
)
from app.services import chart_marks as chart_marks_service
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io, run_cache_io_best_effort
from app.services.llm_explainer import enhance_stock_answer
from app.services.research import answer_stock_question
from app.services.stock_insights import rule_definitions
from app.services.workbench_context import WorkbenchContext, WorkbenchContextCache
from app.utils.symbols import standard_symbol
from app.utils.time import now_text
from app.workflows.market_overview import market_overview, strong_stock_watch
from app.workflows.stock_analysis import analyze_individual_stock, review_individual_stock, stock_minute_analysis
from app.workflows.stock_lookup import confirmed_stock_profile as _confirmed_stock_profile
from app.workflows.stock_lookup import match_industry
from app.workflows.workbench_pipeline import build_workbench_context as _build_workbench_context


WORKBENCH_CHART_MARK_LIMIT = 80
WORKBENCH_ALERT_RULE_LIMIT = 100
WORKBENCH_ALERT_EVENT_LIMIT = 20
WORKBENCH_NOTE_LIMIT = 50


@dataclass(frozen=True)
class StockWorkbenchLocalState:
    chart_marks: ChartMarkSummary
    alert_rules: list[AlertRuleItem]
    alert_events: list[AlertEventItem]
    notes: list[StockNoteItem]
    warnings: list[WorkbenchDataWarning]


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
    context = await stock_workbench_context(datahub, symbol)
    normalized = _workbench_symbol(context.insights.overview.symbol)
    advice_warning = await _ensure_advice_snapshot(datahub, context)
    local_state = await _workbench_local_state(datahub, normalized, context)
    warnings = [item for item in [advice_warning, *local_state.warnings] if item is not None]
    return _stock_workbench_response(context, normalized, local_state, warnings)


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
