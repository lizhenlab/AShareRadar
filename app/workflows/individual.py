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
from app.models.advice_change import (
    CONCLUSION_BASIS,
    MODEL_VERSION,
    SNAPSHOT_CONTRACT_VERSION,
    conclusion_identity,
)
from app.models.market import DAILY_KLINE_CONTRACT_VERSION, Kline
from app.models.reviews import (
    ResearchQueueRefreshItem,
    ResearchQueueRefreshSummary,
)
from app.models.rule_versions import RULE_VERSION
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
from app.services.trading_calendar import DAILY_KLINE_PUBLISH_TIME, is_trading_day
from app.services.stock_insights import rule_definitions
from app.services.workbench_context import WorkbenchContext, WorkbenchContextCache
from app.utils.symbols import standard_symbol
from app.utils.audit_time import audit_datetime_to_text, audit_now_text as now_text
from app.utils.clock import market_now_naive, performance_now
from app.utils.market_data import valid_kline
from app.utils.market_time import market_local_naive, normalize_market_datetime
from app.utils.provider_errors import sanitize_provider_error
from app.workflows.market_overview import market_overview, strong_stock_watch
from app.workflows.stock_analysis import analyze_individual_stock, review_individual_stock, stock_minute_analysis
from app.workflows.stock_lookup import confirmed_stock_profile as _confirmed_stock_profile
from app.workflows.stock_lookup import match_industry
from app.workflows.workbench_pipeline import build_workbench_context as _build_workbench_context


WORKBENCH_CHART_MARK_LIMIT = 80
WORKBENCH_ALERT_RULE_LIMIT = 100
WORKBENCH_ALERT_EVENT_LIMIT = 20
WORKBENCH_NOTE_LIMIT = 50
ACTIVE_RESEARCH_REFRESH_LIMIT = 20
MAX_ACTIVE_RESEARCH_REFRESH_LIMIT = 100
MIN_ACTIVE_RESEARCH_QUALITY_SCORE = 50
INVALID_RESEARCH_CONTRACT_VERSIONS = frozenset({"", "unknown", "legacy"})
ACTIVE_RESEARCH_CURSOR_ATTR = "_active_research_queue_cursor"


@dataclass(frozen=True)
class StockWorkbenchLocalState:
    chart_marks: ChartMarkSummary
    alert_rules: list[AlertRuleItem]
    alert_events: list[AlertEventItem]
    notes: list[StockNoteItem]
    warnings: list[WorkbenchDataWarning]


async def refresh_active_research_queue(
    datahub: DataHub,
    *,
    now: datetime | None = None,
    limit: int = ACTIVE_RESEARCH_REFRESH_LIMIT,
) -> ResearchQueueRefreshSummary:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("主动研究刷新上限必须是正整数")
    bounded_limit = min(limit, MAX_ACTIVE_RESEARCH_REFRESH_LIMIT)
    current = market_local_naive(now) if now is not None else market_now_naive()
    started_at = audit_datetime_to_text(current)
    if not _active_research_refresh_window(current):
        return ResearchQueueRefreshSummary(
            started_at=started_at,
            deferred=True,
            reason_code="not_after_close",
        )

    data_date = current.date().isoformat()
    selection = await run_cache_io(datahub.cache.watchlist_symbol_selection)
    excluded_symbols = set(selection.excluded_symbols)
    active_symbols = [symbol for symbol in selection.active_symbols if symbol not in excluded_symbols]
    latest_by_symbol = await run_cache_io(
        datahub.cache.latest_advice_timeline_by_symbols,
        active_symbols,
    )
    candidate_symbols = _active_research_candidate_order(
        datahub.cache,
        active_symbols,
        latest_by_symbol,
        data_date=data_date,
    )
    selected_symbols = candidate_symbols[:bounded_limit]
    _advance_active_research_cursor(
        datahub.cache,
        active_count=len(active_symbols),
        attempted_count=len(selected_symbols),
    )
    items: list[ResearchQueueRefreshItem] = []
    for symbol in selected_symbols:
        item = await _refresh_active_research_symbol(datahub, symbol, data_date=data_date)
        items.append(item)
        await asyncio.sleep(0)
    return ResearchQueueRefreshSummary(
        started_at=started_at,
        data_date=data_date,
        active_count=len(active_symbols),
        selected_count=len(selected_symbols),
        saved_count=sum(item.status == "saved" for item in items),
        unchanged_count=sum(item.status == "unchanged" for item in items),
        skipped_count=sum(item.status == "skipped" for item in items),
        failed_count=sum(item.status == "failed" for item in items),
        items=items,
    )


def _active_research_refresh_window(now: datetime) -> bool:
    return is_trading_day(now.date()) and now.time() >= DAILY_KLINE_PUBLISH_TIME


async def _refresh_active_research_symbol(
    datahub: DataHub,
    symbol: str,
    *,
    data_date: str,
) -> ResearchQueueRefreshItem:
    try:
        normalized = standard_symbol(symbol)
        analysis = await analyze_individual_stock(datahub, normalized, persist_history=False)
        rejection = _active_research_snapshot_rejection(analysis, normalized, data_date)
        if rejection is not None:
            return ResearchQueueRefreshItem(
                symbol=normalized,
                status="skipped",
                reason_code=rejection,
                data_date=data_date,
            )
        latest = await run_cache_io(datahub.cache.advice_timeline, normalized, limit=1)
        if latest and _advice_snapshot_is_current(latest[0], analysis, data_date):
            return ResearchQueueRefreshItem(
                symbol=normalized,
                status="unchanged",
                reason_code="already_current",
                advice_id=latest[0].id,
                data_date=data_date,
            )
        snapshot_market_time = f"{data_date} {DAILY_KLINE_PUBLISH_TIME.strftime('%H:%M:%S')}"
        saved = await run_cache_io(
            datahub.cache.save_advice_snapshot,
            analysis,
            snapshot_market_time=snapshot_market_time,
        )
        return ResearchQueueRefreshItem(
            symbol=normalized,
            status="saved",
            advice_id=saved.id,
            data_date=data_date,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return ResearchQueueRefreshItem(
            symbol=str(symbol),
            status="failed",
            reason_code="analysis_failed",
            data_date=data_date,
            message=_short_research_refresh_error(
                exc,
                sensitive_values=_background_sensitive_values(datahub),
            ),
        )


def _active_research_snapshot_rejection(analysis, expected_symbol: str, data_date: str):
    if _analysis_data_date(analysis) != data_date:
        return "stale_data_date"
    quality = analysis.data_quality
    kline_quality = quality.kline_quality
    if (
        quality.score < MIN_ACTIVE_RESEARCH_QUALITY_SCORE
        or kline_quality is None
        or kline_quality.last_date != data_date
        or kline_quality.days_behind_expected != 0
        or kline_quality.fallback_used
        or analysis.quote.fallback_used
    ):
        return "low_data_quality"
    anchor = _latest_research_kline(analysis.klines, data_date)
    if anchor is None:
        return "stale_data_date"
    observed_symbol = standard_symbol(f"{analysis.quote.code}.{analysis.quote.market}")
    if (
        observed_symbol != expected_symbol
        or anchor.adjustment_mode != "qfq"
        or anchor.data_version in INVALID_RESEARCH_CONTRACT_VERSIONS
        or anchor.contract_version != DAILY_KLINE_CONTRACT_VERSION
        or anchor.fallback_used
        or RULE_VERSION in INVALID_RESEARCH_CONTRACT_VERSIONS
        or SNAPSHOT_CONTRACT_VERSION in INVALID_RESEARCH_CONTRACT_VERSIONS
        or not str(analysis.action_advice.action or "").strip()
    ):
        return "invalid_rule_contract"
    return None


def _analysis_data_date(analysis) -> str | None:
    normalized = normalize_market_datetime(analysis.quote.timestamp)
    return normalized[:10] if normalized is not None else None


def _latest_research_kline(rows: list[Kline], data_date: str) -> Kline | None:
    candidates = [row for row in rows if row.date == data_date and valid_kline(row)]
    return candidates[-1] if candidates else None


def _active_research_candidate_order(
    cache: object,
    active_symbols: list[str],
    latest_by_symbol: dict[str, object],
    *,
    data_date: str,
) -> list[str]:
    if not active_symbols:
        return []
    cursor = _active_research_cursor(cache, len(active_symbols))
    rotated = active_symbols[cursor:] + active_symbols[:cursor]
    rotated_rank = {symbol: index for index, symbol in enumerate(rotated)}

    def candidate_key(symbol: str) -> tuple[int, str, int]:
        snapshot = latest_by_symbol.get(symbol)
        is_current = snapshot is not None and _advice_snapshot_contract_is_current(
            snapshot,
            data_date,
        )
        snapshot_time = (
            normalize_market_datetime(getattr(snapshot, "market_time", None))
            if snapshot is not None
            else None
        )
        return (int(is_current), snapshot_time or "", rotated_rank[symbol])

    return sorted(active_symbols, key=candidate_key)


def _active_research_cursor(cache: object, active_count: int) -> int:
    try:
        cursor = int(getattr(cache, ACTIVE_RESEARCH_CURSOR_ATTR, 0))
    except (TypeError, ValueError):
        cursor = 0
    return cursor % active_count


def _advance_active_research_cursor(
    cache: object,
    *,
    active_count: int,
    attempted_count: int,
) -> None:
    if active_count <= 0 or attempted_count <= 0:
        return
    cursor = (_active_research_cursor(cache, active_count) + attempted_count) % active_count
    setattr(cache, ACTIVE_RESEARCH_CURSOR_ATTR, cursor)


def _advice_snapshot_is_current(snapshot, analysis, data_date: str) -> bool:
    anchor = _latest_research_kline(analysis.klines, data_date)
    if anchor is None:
        return False
    snapshot_identity = conclusion_identity(snapshot)
    analysis_identity = conclusion_identity(_analysis_conclusion_values(analysis))
    return bool(
        _advice_snapshot_contract_is_current(snapshot, data_date)
        and snapshot_identity is not None
        and snapshot_identity == analysis_identity
        and snapshot.kline_adjustment_mode == anchor.adjustment_mode
        and snapshot.kline_data_version == anchor.data_version
        and snapshot.kline_contract_version == anchor.contract_version
        and _same_research_price(snapshot.kline_anchor_close, anchor.close)
    )


def _advice_snapshot_contract_is_current(snapshot, data_date: str) -> bool:
    market_time = normalize_market_datetime(snapshot.market_time)
    return bool(
        market_time is not None
        and market_time[:10] == data_date
        and snapshot.kline_anchor_date == data_date
        and snapshot.kline_adjustment_mode == "qfq"
        and snapshot.kline_data_version not in INVALID_RESEARCH_CONTRACT_VERSIONS
        and snapshot.rule_version == RULE_VERSION
        and snapshot.snapshot_contract_version == SNAPSHOT_CONTRACT_VERSION
        and snapshot.kline_contract_version == DAILY_KLINE_CONTRACT_VERSION
    )


def _analysis_conclusion_values(analysis) -> dict[str, object]:
    return {
        "action": analysis.action_advice.action,
        "confidence": analysis.action_advice.confidence,
        "trend_score": analysis.trend_score,
        "trend_label": analysis.trend_label,
        "risk_level": analysis.risk_level,
        "support": analysis.support,
        "resistance": analysis.resistance,
        "data_quality_score": analysis.data_quality.score,
        "data_quality_level": analysis.data_quality.level,
        "data_quality_source": analysis.data_quality.source,
        "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
        "conclusion_basis": CONCLUSION_BASIS,
        "rule_version": RULE_VERSION,
        "model_version": MODEL_VERSION,
    }


def _same_research_price(value: object, expected: float) -> bool:
    try:
        return float(value) == float(expected)
    except (TypeError, ValueError):
        return False


def _short_research_refresh_error(
    exc: Exception,
    *,
    sensitive_values: tuple[object, ...] = (),
) -> str:
    message = " ".join(
        sanitize_provider_error(exc, sensitive_values=sensitive_values).split()
    ).strip()
    return (message or exc.__class__.__name__)[:160]


def _background_sensitive_values(datahub: object) -> tuple[object, ...]:
    cache = getattr(datahub, "cache", None)
    settings = getattr(datahub, "settings", None) or getattr(cache, "settings", None)
    if settings is None:
        return ()
    values = (
        getattr(settings, "tushare_token", None),
        getattr(settings, "llm_api_key", None),
        getattr(settings, "llm_base_url", None),
    )
    return tuple(value for value in values if value not in (None, ""))


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
