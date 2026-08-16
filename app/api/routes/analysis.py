from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import get_app_settings, get_datahub
from app.api.errors import (
    no_store_http_exception,
    no_store_internal_exception,
    run_api,
    run_sync_api_async,
)
from app.config import Settings
from app.models.analysis import (
    AnalysisResult,
    IndividualReview,
)
from app.models.workbench import (
    MarketOverview,
    StrongStockWatchResponse,
)
from app.services.datahub import DataHub
from app.utils.symbols import standard_a_share_stock_symbol
from app.workflows.individual import analyze_individual_stock, market_overview, review_individual_stock, strong_stock_watch


router = APIRouter()


@router.get("/api/analyze", response_model=AnalysisResult)
async def analyze(
    response: Response,
    symbol: str = Query("600519", description="6位A股代码"),
    datahub: DataHub = Depends(get_datahub),
) -> AnalysisResult:
    response.headers["Cache-Control"] = "no-store"
    try:
        normalized = await run_sync_api_async(lambda: standard_a_share_stock_symbol(symbol))
        return await run_api(lambda: analyze_individual_stock(datahub, normalized, persist_history=False))
    except HTTPException as exc:
        raise no_store_http_exception(exc) from exc
    except Exception as exc:
        raise no_store_internal_exception(exc) from exc


@router.get("/api/review", response_model=IndividualReview)
async def review(
    response: Response,
    symbol: str = Query("600519", description="6位A股代码"),
    period_days: int = Query(60, ge=20, le=240),
    datahub: DataHub = Depends(get_datahub),
) -> IndividualReview:
    response.headers["Cache-Control"] = "no-store"
    normalized = await run_sync_api_async(lambda: standard_a_share_stock_symbol(symbol))
    return await run_api(lambda: review_individual_stock(datahub, normalized, period_days))


@router.get("/api/strong-stocks", response_model=StrongStockWatchResponse)
async def strong_stocks(
    symbols: str | None = None,
    datahub: DataHub = Depends(get_datahub),
    settings: Settings = Depends(get_app_settings),
) -> StrongStockWatchResponse:
    return await run_api(lambda: strong_stock_watch(datahub, settings, symbols))


@router.get("/api/leaderboard", response_model=StrongStockWatchResponse)
async def leaderboard(
    symbols: str | None = None,
    datahub: DataHub = Depends(get_datahub),
    settings: Settings = Depends(get_app_settings),
) -> StrongStockWatchResponse:
    return await strong_stocks(symbols=symbols, datahub=datahub, settings=settings)


@router.get("/api/market", response_model=MarketOverview)
async def market(
    datahub: DataHub = Depends(get_datahub),
    settings: Settings = Depends(get_app_settings),
) -> MarketOverview:
    return await run_api(lambda: market_overview(datahub, settings))
