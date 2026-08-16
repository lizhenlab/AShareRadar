from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import get_datahub
from app.api.errors import (
    no_store_http_exception,
    no_store_internal_exception,
    run_api,
    run_sync_api_async,
)
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
from app.models.individual_probability import IndividualUpsideProbabilityReport
from app.models.research import (
    AlphaEvidenceReport,
    ChipAnalysis,
    EventDigestReport,
    EvidenceChainReport,
    FactorLabReport,
    LeadershipReport,
    MarketRegimeReport,
    MinuteAnalysisReport,
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
from app.models.workbench import (
    StockWorkbench,
)
from app.services.datahub import DataHub
from app.services.individual_probability import (
    individual_probability_store_for_cache_path,
    project_individual_upside_probability,
)
from app.services.individual_probability_artifact import IndividualProbabilityArtifactError
from app.services.datahub_runtime import run_cache_io
from app.utils.symbols import standard_a_share_stock_symbol
from app.workflows.individual import (
    stock_abnormal_events,
    stock_alpha_evidence,
    stock_chip_analysis,
    stock_diagnosis,
    stock_event_digest,
    stock_events,
    stock_evidence_chain,
    stock_factor_lab,
    stock_factors,
    stock_feature_snapshot,
    stock_financial_health,
    stock_fund_flow,
    stock_insight_bundle,
    stock_leadership,
    stock_lhb,
    stock_market_regime,
    stock_minute_analysis,
    stock_order_pressure,
    stock_overview,
    stock_peer_comparison,
    stock_qa_report,
    stock_question_answer,
    stock_replay,
    stock_risk_radar,
    stock_rule_definitions,
    stock_rule_matches,
    stock_strategy_cards,
    stock_t_strategy,
    stock_theme_context,
    stock_valuation,
    stock_workbench,
)


router = APIRouter()
T = TypeVar("T")
StockHandler = Callable[[DataHub, str], Awaitable[T]]


def _register_stock_get(path: str, response_model: Any, handler: StockHandler[Any]) -> None:
    async def endpoint(
        response: Response,
        symbol: str = Query("600519", description="6位A股代码"),
        datahub: DataHub = Depends(get_datahub),
    ) -> Any:
        no_store = path == "/api/stock/workbench"
        if no_store:
            response.headers["Cache-Control"] = "no-store"
        try:
            normalized = await run_sync_api_async(lambda: standard_a_share_stock_symbol(symbol))
            return await run_api(lambda: handler(datahub, normalized))
        except HTTPException as exc:
            if no_store:
                raise no_store_http_exception(exc) from exc
            raise
        except Exception as exc:
            if no_store:
                raise no_store_internal_exception(exc) from exc
            raise

    endpoint.__name__ = f"{handler.__name__}_endpoint"
    router.add_api_route(path, endpoint, methods=["GET"], response_model=response_model)


for path, response_model, handler in [
    ("/api/stock/insights", StockInsightBundle, stock_insight_bundle),
    ("/api/stock/workbench", StockWorkbench, stock_workbench),
    ("/api/stock/features", FeatureSnapshot, stock_feature_snapshot),
    ("/api/stock/factor-lab", FactorLabReport, stock_factor_lab),
    ("/api/stock/market-regime", MarketRegimeReport, stock_market_regime),
    ("/api/stock/alpha-evidence", AlphaEvidenceReport, stock_alpha_evidence),
    ("/api/stock/diagnosis", StockDiagnosis, stock_diagnosis),
    ("/api/stock/evidence-chain", EvidenceChainReport, stock_evidence_chain),
    ("/api/stock/qa-report", StockQaReport, stock_qa_report),
    ("/api/stock/event-digest", EventDigestReport, stock_event_digest),
    ("/api/stock/peer-comparison", PeerComparisonReport, stock_peer_comparison),
    ("/api/stock/t-strategy", TStrategyAssistantReport, stock_t_strategy),
    ("/api/stock/risk-radar", RiskRadarReport, stock_risk_radar),
    ("/api/stock/chips", ChipAnalysis, stock_chip_analysis),
    ("/api/stock/leadership", LeadershipReport, stock_leadership),
    ("/api/stock/theme-context", ThemeContextReport, stock_theme_context),
    ("/api/stock/replay", StockReplayAnalysis, stock_replay),
    ("/api/stock/overview", StockOverview, stock_overview),
    ("/api/stock/factors", list[FactorScore], stock_factors),
    ("/api/stock/fund-flow", FundFlowAnalysis, stock_fund_flow),
    ("/api/stock/order-pressure", OrderPressure, stock_order_pressure),
    ("/api/stock/events", StockEventSummary, stock_events),
    ("/api/stock/strategy-cards", list[StrategyCard], stock_strategy_cards),
    ("/api/stock/financial-health", FinancialHealth, stock_financial_health),
    ("/api/stock/valuation", ValuationAnalysis, stock_valuation),
    ("/api/stock/lhb", LhbSummary, stock_lhb),
    ("/api/stock/abnormal-events", AbnormalEventSummary, stock_abnormal_events),
    ("/api/stock/rule-matches", StockRuleMatchSummary, stock_rule_matches),
]:
    _register_stock_get(path, response_model, handler)


@router.get("/api/stock/minute-analysis", response_model=MinuteAnalysisReport)
async def minute_analysis(
    symbol: str = Query("600519", description="6位A股代码"),
    interval: Literal["1m", "5m", "15m", "30m", "60m"] = Query(
        "5m",
        description="分钟周期：1m/5m/15m/30m/60m",
    ),
    limit: int = Query(120, ge=20, le=500),
    datahub: DataHub = Depends(get_datahub),
) -> MinuteAnalysisReport:
    normalized = await run_sync_api_async(lambda: standard_a_share_stock_symbol(symbol))
    return await run_api(lambda: stock_minute_analysis(datahub, normalized, interval=interval, limit=limit))


@router.post("/api/stock/ask", response_model=StockQuestionAnswer)
async def ask_stock(
    payload: StockQuestionInput,
    datahub: DataHub = Depends(get_datahub),
) -> StockQuestionAnswer:
    normalized = await run_sync_api_async(lambda: standard_a_share_stock_symbol(payload.symbol))
    return await run_api(
        lambda: stock_question_answer(datahub, payload.model_copy(update={"symbol": normalized}))
    )


@router.get("/api/rules", response_model=list[RuleDefinition])
async def rules() -> list[RuleDefinition]:
    return stock_rule_definitions()


@router.get(
    "/api/stock/upside-probability",
    response_model=IndividualUpsideProbabilityReport,
)
async def upside_probability(
    response: Response,
    symbol: str = Query("600519", description="6位A股代码"),
    datahub: DataHub = Depends(get_datahub),
) -> IndividualUpsideProbabilityReport:
    """Return compact artifact projection without request-time model fitting."""
    response.headers["Cache-Control"] = "no-store"
    try:
        normalized = await run_sync_api_async(lambda: _individual_probability_symbol(symbol))
        store = individual_probability_store_for_cache_path(datahub.cache.path)
        assessment = await run_cache_io(store.latest)
    except IndividualProbabilityArtifactError as exc:
        raise HTTPException(
            status_code=409,
            detail="个股上涨概率证据损坏，拒绝回退旧版本",
            headers={"Cache-Control": "no-store"},
        ) from exc
    except HTTPException as exc:
        raise no_store_http_exception(exc) from exc
    except Exception as exc:
        raise no_store_internal_exception(exc) from exc
    try:
        return await run_sync_api_async(
            lambda: project_individual_upside_probability(normalized, assessment)
        )
    except HTTPException as exc:
        raise no_store_http_exception(exc) from exc
    except Exception as exc:
        raise no_store_internal_exception(exc) from exc


def _individual_probability_symbol(value: str) -> str:
    try:
        return standard_a_share_stock_symbol(value)
    except ValueError as exc:
        text = str(value or "").strip().upper()
        if len(text) == 9 and text[:6].isdigit() and text[6:] in {".SH", ".SZ", ".BJ"} and text[:6] != "000000":
            raise ValueError("股票代码与 A 股交易所不一致") from exc
        raise
