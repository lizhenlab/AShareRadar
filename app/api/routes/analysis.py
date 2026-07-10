from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_app_settings, get_datahub
from app.api.errors import run_api
from app.config import Settings
from app.models.schemas import AnalysisResult, IndividualReview, MarketOverview, StrongStockWatchResponse
from app.services.datahub import DataHub
from app.workflows.individual import analyze_individual_stock, market_overview, review_individual_stock, strong_stock_watch


router = APIRouter()


@router.get("/api/analyze", response_model=AnalysisResult)
async def analyze(
    symbol: str = Query("600519", description="6位A股代码"),
    datahub: DataHub = Depends(get_datahub),
) -> AnalysisResult:
    return await run_api(lambda: analyze_individual_stock(datahub, symbol))


@router.get("/api/review", response_model=IndividualReview)
async def review(
    symbol: str = Query("600519", description="6位A股代码"),
    period_days: int = Query(60, ge=20, le=240),
    datahub: DataHub = Depends(get_datahub),
) -> IndividualReview:
    return await run_api(lambda: review_individual_stock(datahub, symbol, period_days))


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
