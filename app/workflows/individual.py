from __future__ import annotations

import asyncio

from app.config import Settings
from app.models.schemas import (
    AbnormalEventSummary,
    AlphaEvidenceReport,
    AnalysisResult,
    ChipAnalysis,
    EventDigestReport,
    EvidenceChainReport,
    FactorLabReport,
    FactorScore,
    FeatureSnapshot,
    FinancialHealth,
    FundFlowAnalysis,
    IndividualReview,
    LeadershipReport,
    LhbSummary,
    MarketRegimeReport,
    MarketOverview,
    MinuteAnalysisReport,
    OrderPressure,
    PlateItem,
    RiskRadarReport,
    RuleDefinition,
    StockEventSummary,
    StockInfo,
    StockInsightBundle,
    StockOverview,
    StockDiagnosis,
    StockQuestionAnswer,
    StockQuestionInput,
    StockQaReport,
    StockReplayAnalysis,
    StockWorkbench,
    StockRuleMatchSummary,
    ThemeContextReport,
    StrategyCard,
    TStrategyAssistantReport,
    ValuationAnalysis,
)
from app.services.analysis import build_analysis, build_strong_stock_watch
from app.services.datahub import DataHub
from app.services.llm_explainer import enhance_stock_answer
from app.services.market_sampling import (
    STRONG_STOCK_SAMPLE_LIMIT,
    market_breadth_quotes as _market_breadth_quotes,
    market_breadth_symbols as _market_breadth_symbols,
    peer_quotes as _peer_quotes,
    unique_standard_symbols as _unique_standard_symbols,
)
from app.services.minute_analysis import build_minute_analysis_report, build_unavailable_minute_analysis_report
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
    build_risk_reward_report,
    build_risk_radar_report,
    build_signal_validation_report,
    build_stock_diagnosis,
    build_theme_context_report,
    build_stock_qa_report,
    answer_stock_question,
    build_t_strategy_assistant_report,
    build_timeframe_alignment_report,
)
from app.services.review import build_individual_review
from app.services.stock_insights import build_stock_insight_bundle, rule_definitions
from app.services.workbench_context import WorkbenchContext, WorkbenchContextCache
from app.utils.errors import NotFoundError
from app.utils.symbols import normalize_symbol
from app.utils.time import now_text


_WORKBENCH_CONTEXTS = WorkbenchContextCache()


async def analyze_individual_stock(datahub: DataHub, symbol: str, persist_history: bool = True) -> AnalysisResult:
    code, market = normalize_symbol(symbol)
    standard = f"{code}.{market.upper()}"
    profile = await _confirmed_stock_profile(datahub, standard)
    quote_data, klines, plates = await asyncio.gather(
        datahub.quote(symbol),
        datahub.kline(symbol, 120),
        datahub.plate_rank(limit=20),
    )
    data_quality = await datahub.assess_quote_quality(quote_data, klines)
    industry = match_industry(profile, plates)
    review = build_individual_review(quote_data, klines, period_days=60)
    quote_history = datahub.cache.quote_history(f"{quote_data.code}.{quote_data.market}", limit=120)
    peer_quotes = await _peer_quotes(datahub, profile, f"{quote_data.code}.{quote_data.market}")
    result = build_analysis(
        quote_data,
        klines,
        stock_profile=profile,
        industry_context=industry,
        review=review,
        data_quality=data_quality,
        quote_history=quote_history,
        peer_quotes=peer_quotes,
    )
    if persist_history:
        datahub.cache.save_advice_snapshot(result)
    return result


async def stock_workbench_context(
    datahub: DataHub,
    symbol: str,
    *,
    use_cache: bool = True,
    context_cache: WorkbenchContextCache | None = None,
) -> WorkbenchContext:
    cache = context_cache or getattr(datahub, "workbench_contexts", None) or _WORKBENCH_CONTEXTS
    return await cache.get(symbol, lambda normalized: _build_workbench_context(datahub, normalized), use_cache=use_cache)


async def _build_workbench_context(datahub: DataHub, symbol: str) -> WorkbenchContext:
    analysis = await analyze_individual_stock(datahub, symbol, persist_history=False)
    breadth_quotes = await _market_breadth_quotes(datahub)
    order_book = None
    order_book_error = None
    futu_provider = datahub.providers.get("futu")
    futu_capability = provider_capability(futu_provider) if futu_provider else None
    futu_enabled = bool(futu_capability and futu_capability.enabled)
    if futu_enabled:
        try:
            order_book = await datahub.order_book(symbol)
        except Exception as exc:
            order_book_error = str(exc)
    else:
        order_book_error = "Futu OpenAPI 未启用，盘口压力使用行情区间估算。"
    insights = build_stock_insight_bundle(analysis, order_book=order_book, order_book_error=order_book_error)
    feature_snapshot = build_feature_snapshot(analysis, insights)
    concepts = await datahub.stock_concepts(symbol, limit=8)
    theme_context = build_theme_context_report(analysis, feature_snapshot, concepts)
    chip_analysis = build_chip_analysis(analysis, feature_snapshot)
    leadership = build_leadership_report(analysis, insights, feature_snapshot, concepts)
    factor_lab = build_factor_lab_report(analysis, insights, feature_snapshot, chip_analysis, leadership)
    market_breadth = build_market_breadth_snapshot(breadth_quotes)
    market_regime = build_market_regime_report(analysis, insights, feature_snapshot, factor_lab, market_breadth)
    timeframe_alignment = build_timeframe_alignment_report(analysis, feature_snapshot, factor_lab)
    signal_validation = build_signal_validation_report(analysis, feature_snapshot, factor_lab, market_regime, timeframe_alignment)
    risk_reward = build_risk_reward_report(analysis, feature_snapshot, factor_lab, market_regime, signal_validation, timeframe_alignment)
    alpha_evidence = build_alpha_evidence_report(analysis, insights, feature_snapshot, factor_lab, market_regime, timeframe_alignment, risk_reward)
    diagnosis = build_stock_diagnosis(
        analysis,
        insights,
        feature_snapshot,
        alpha_evidence,
        factor_lab,
        market_regime,
        signal_validation,
        risk_reward,
        timeframe_alignment,
    )
    evidence_chain = build_evidence_chain_report(diagnosis, alpha_evidence, signal_validation, risk_reward)
    t_strategy = build_t_strategy_assistant_report(analysis, feature_snapshot, market_regime, signal_validation)
    qa_report = build_stock_qa_report(analysis, diagnosis, market_regime, risk_reward, t_strategy, theme_context)
    event_digest = build_event_digest_report(insights)
    peer_comparison = build_peer_comparison_report(analysis, insights, feature_snapshot)
    risk_radar = build_risk_radar_report(analysis, insights, feature_snapshot, market_regime, risk_reward, timeframe_alignment)
    replay = build_replay_analysis(analysis)
    return WorkbenchContext(
        analysis=analysis,
        insights=insights,
        feature_snapshot=feature_snapshot,
        factor_lab=factor_lab,
        market_regime=market_regime,
        signal_validation=signal_validation,
        risk_reward=risk_reward,
        timeframe_alignment=timeframe_alignment,
        alpha_evidence=alpha_evidence,
        diagnosis=diagnosis,
        evidence_chain=evidence_chain,
        qa_report=qa_report,
        event_digest=event_digest,
        peer_comparison=peer_comparison,
        t_strategy=t_strategy,
        risk_radar=risk_radar,
        chip_analysis=chip_analysis,
        leadership=leadership,
        theme_context=theme_context,
        replay=replay,
        order_book_error=order_book_error,
    )


async def stock_insight_bundle(datahub: DataHub, symbol: str) -> StockInsightBundle:
    return (await stock_workbench_context(datahub, symbol)).insights


async def stock_workbench(datahub: DataHub, symbol: str) -> StockWorkbench:
    context = await stock_workbench_context(datahub, symbol)
    from app.services.chart_marks import build_chart_marks_from_context

    normalized = context.insights.overview.symbol
    if not context.advice_snapshot_saved:
        datahub.cache.save_advice_snapshot(context.analysis)
        context.advice_snapshot_saved = True
    chart_marks = build_chart_marks_from_context(datahub, normalized, context.insights, limit=80)
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
        chart_marks=chart_marks,
        alert_rules=datahub.cache.alert_rules(symbol=normalized, include_disabled=True, limit=100),
        alert_events=datahub.cache.alert_events(symbol=normalized, limit=20),
        notes=datahub.cache.stock_notes(normalized, limit=50),
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


async def stock_minute_analysis(datahub: DataHub, symbol: str, interval: str = "5m", limit: int = 120) -> MinuteAnalysisReport:
    normalized = normalize_symbol(symbol)
    standard = f"{normalized[0]}.{normalized[1].upper()}"
    try:
        rows = await datahub.minute_kline(standard, interval=interval, limit=limit)
    except RuntimeError as exc:
        datahub.cache.log_event("fallback", f"分钟分析不可用：{standard} {interval}；{exc}")
        return build_unavailable_minute_analysis_report(standard, interval=interval, reason=str(exc))
    return build_minute_analysis_report(standard, rows, interval=interval)


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


async def review_individual_stock(datahub: DataHub, symbol: str, period_days: int) -> IndividualReview:
    code, market = normalize_symbol(symbol)
    await _confirmed_stock_profile(datahub, f"{code}.{market.upper()}")
    quote_data, klines = await asyncio.gather(datahub.quote(symbol), datahub.kline(symbol, max(period_days, 120)))
    return build_individual_review(quote_data, klines, period_days=period_days)


async def strong_stock_watch(datahub: DataHub, settings: Settings, symbols: str | None = None) -> dict[str, object]:
    if symbols:
        symbol_list = _unique_standard_symbols(item.strip() for item in symbols.split(",") if item.strip())
        scope = "自定义列表"
    else:
        watch_symbols = datahub.cache.watchlist_symbols()
        breadth_symbols = await _market_breadth_symbols(datahub)
        symbol_list = _unique_standard_symbols([*watch_symbols, *settings.seed_symbols, *breadth_symbols])[:STRONG_STOCK_SAMPLE_LIMIT]
        scope = "自选股 + 默认观察池 + 股票池分层抽样"
        if not symbol_list:
            symbol_list = list(settings.seed_symbols)
            scope = "默认观察池"
    quotes_data = await datahub.quotes(symbol_list)
    kline_rows = await asyncio.gather(*(datahub.kline(f"{item.code}.{item.market}", 80) for item in quotes_data))
    kline_map = {quote_data.code: rows for quote_data, rows in zip(quotes_data, kline_rows)}
    items = build_strong_stock_watch(quotes_data, kline_map)
    return {"updated_at": quotes_data[0].timestamp if quotes_data else "", "items": items, "scope": scope, "sample_count": len(quotes_data)}


async def _confirmed_stock_profile(datahub: DataHub, symbol: str) -> StockInfo | None:
    try:
        profile = await datahub.stock_profile(symbol)
    except RuntimeError as exc:
        raise RuntimeError(f"股票池暂不可用，无法确认股票代码：{symbol}；{exc}") from exc
    if profile is None:
        raise NotFoundError(f"股票代码不存在或当前股票池不支持：{symbol}")
    return profile


async def market_overview(datahub: DataHub, settings: Settings) -> MarketOverview:
    index_symbols = ["sh000001", "sz399001", "sz399006"]
    stock_symbols = list(settings.seed_symbols)
    index_quotes, stock_quotes = await asyncio.gather(datahub.quotes(index_symbols), datahub.quotes(stock_symbols))
    kline_rows = await asyncio.gather(*(datahub.kline(f"{item.code}.{item.market}", 80) for item in stock_quotes))
    strong = build_strong_stock_watch(stock_quotes, {item.code: rows for item, rows in zip(stock_quotes, kline_rows)})
    return MarketOverview(
        indices=index_quotes,
        strong_stocks=strong[:5],
        risk_note="本平台只用于个股研究和建议辅助，不做组合策略、不自动交易；实盘需结合个人仓位和风险承受能力。",
    )


def match_industry(profile: StockInfo | None, plates: list[PlateItem]) -> PlateItem | None:
    if not profile or not profile.industry:
        return None
    for item in plates:
        if item.name == profile.industry:
            return item
    return None
