from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import get_market_scanner
from app.api.errors import run_api, run_sync_api_async
from app.models.market_scan import (
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanSort,
    MarketScanSortOrder,
    MarketScanStartRequest,
    MarketScanStartResponse,
)
from app.services.market_scan_manager import MarketScanManager


router = APIRouter()
MarketCode = Literal["SH", "SZ", "BJ"]
MarketScanStatusFilter = MarketScanResultStatus | Literal["all"]


@router.post("/api/market-scans", response_model=MarketScanStartResponse, status_code=202)
async def create_market_scan(
    payload: MarketScanStartRequest | None = None,
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanStartResponse:
    request = payload or MarketScanStartRequest()
    return await run_api(lambda: scanner.create_scan(as_of=request.as_of, trigger="manual"))


@router.get("/api/market-scans/latest", response_model=MarketScanRun | None)
async def latest_market_scan(
    response: Response,
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanRun | None:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(scanner.latest_run)


@router.get("/api/market-scans", response_model=MarketScanRunPage)
async def market_scan_runs(
    response: Response,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanRunPage:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: scanner.runs(page=page, page_size=page_size))


@router.get("/api/market-scans/{run_id}", response_model=MarketScanRun)
async def market_scan_run(
    run_id: int,
    response: Response,
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanRun:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: scanner.run(run_id))


@router.get("/api/market-scans/{run_id}/results", response_model=MarketScanResultPage)
async def market_scan_results(
    run_id: int,
    response: Response,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=200),
    status: MarketScanStatusFilter = Query("success"),
    market: MarketCode | None = Query(None),
    industry: str | None = Query(None, max_length=80),
    is_st: bool | None = Query(None),
    is_new: bool | None = Query(None),
    min_data_quality_score: int | None = Query(None, ge=0, le=100),
    keyword: str | None = Query(None, max_length=80),
    sort: MarketScanSort = Query("rank"),
    order: MarketScanSortOrder = Query("asc"),
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanResultPage:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: scanner.results(
            run_id,
            page=page,
            page_size=page_size,
            status=None if status == "all" else status,
            market=market,
            industry=industry,
            is_st=is_st,
            is_new=is_new,
            min_data_quality_score=min_data_quality_score,
            keyword=keyword,
            sort=sort,
            order=order,
        )
    )


@router.post("/api/market-scans/{run_id}/cancel", response_model=MarketScanRun)
async def cancel_market_scan(
    run_id: int,
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanRun:
    return await run_api(lambda: scanner.cancel_scan(run_id))


@router.post("/api/market-scans/{run_id}/retry", response_model=MarketScanStartResponse, status_code=202)
async def retry_market_scan(
    run_id: int,
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanStartResponse:
    return await run_api(lambda: scanner.retry_scan(run_id))


__all__ = ["router"]
