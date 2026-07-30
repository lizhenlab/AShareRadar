from __future__ import annotations

from datetime import date
from typing import Literal, TypeAlias

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import get_market_scanner
from app.api.errors import run_api, run_sync_api_async
from app.models.market_scan import (
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanMode,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanRunStatus,
    MarketScanSort,
    MarketScanSortOrder,
    MarketScanStartRequest,
    MarketScanStartResponse,
)
from app.services.market_scan_manager import MarketScanManager
from app.services.market_scan_export import XLSX_MEDIA_TYPE, MarketScanExportFilters


router = APIRouter()
MarketCode: TypeAlias = Literal["SH", "SZ", "BJ"]
MarketScanStatusFilter: TypeAlias = MarketScanResultStatus | Literal["all"]
MarketScanRunStatusFilter: TypeAlias = MarketScanRunStatus | Literal["published"]


@router.post("/api/market-scans", response_model=MarketScanStartResponse, status_code=202)
async def create_market_scan(
    payload: MarketScanStartRequest | None = None,
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanStartResponse:
    request = payload or MarketScanStartRequest()
    return await run_api(
        lambda: scanner.create_scan(
            as_of=request.as_of,
            trigger="manual",
            mode=request.mode,
        )
    )


@router.get("/api/market-scans/latest", response_model=MarketScanRun | None)
async def latest_market_scan(
    response: Response,
    mode: MarketScanMode | None = Query(None),
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanRun | None:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: scanner.latest_run(mode=mode))


@router.get("/api/market-scans/latest-published", response_model=MarketScanRun | None)
async def latest_published_market_scan(
    response: Response,
    mode: MarketScanMode | None = Query(None),
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanRun | None:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: scanner.latest_published_run(mode=mode))


@router.get("/api/market-scans", response_model=MarketScanRunPage)
async def market_scan_runs(
    response: Response,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    mode: MarketScanMode | None = Query(None),
    status: MarketScanRunStatusFilter | None = Query(None),
    data_date: date | None = Query(None),
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanRunPage:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: scanner.runs(
            page=page,
            page_size=page_size,
            mode=mode,
            status=status,
            data_date=data_date.isoformat() if data_date is not None else None,
        )
    )


@router.get("/api/market-scans/{run_id}", response_model=MarketScanRun)
async def market_scan_run(
    run_id: int,
    response: Response,
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanRun:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: scanner.run(run_id))


def market_scan_filter_query(
    status: MarketScanStatusFilter = Query("success"),
    market: list[MarketCode] | None = Query(None),
    industry: list[str] | None = Query(None),
    is_st: bool | None = Query(None),
    is_new: bool | None = Query(None),
    min_score: int | None = Query(None, ge=0, le=100),
    max_score: int | None = Query(None, ge=0, le=100),
    min_trend_score: int | None = Query(None, ge=0, le=100),
    max_trend_score: int | None = Query(None, ge=0, le=100),
    min_change_pct: float | None = Query(None, ge=-1000, le=1000),
    max_change_pct: float | None = Query(None, ge=-1000, le=1000),
    min_turnover_rate: float | None = Query(None, ge=0, le=10_000),
    max_turnover_rate: float | None = Query(None, ge=0, le=10_000),
    min_amount: float | None = Query(None, ge=0, le=1_000_000_000_000_000),
    max_amount: float | None = Query(None, ge=0, le=1_000_000_000_000_000),
    min_data_quality_score: int | None = Query(None, ge=0, le=100),
    max_data_quality_score: int | None = Query(None, ge=0, le=100),
    keyword: str | None = Query(None, max_length=80),
    sort: list[MarketScanSort] | None = Query(None),
    order: list[MarketScanSortOrder] | None = Query(None),
) -> MarketScanExportFilters:
    _validate_filter_lists(market, industry, sort, order)
    _validate_filter_ranges(
        ("强势分", min_score, max_score),
        ("趋势分", min_trend_score, max_trend_score),
        ("涨跌幅", min_change_pct, max_change_pct),
        ("换手率", min_turnover_rate, max_turnover_rate),
        ("成交额", min_amount, max_amount),
        ("数据质量", min_data_quality_score, max_data_quality_score),
    )
    sorts = tuple(sort or ("rank",))
    orders: tuple[MarketScanSortOrder, ...] = tuple(
        order or (_default_sort_order(field) for field in sorts)
    )
    return MarketScanExportFilters(
        status=None if status == "all" else status,
        market=tuple(market or ()),
        industry=tuple(_normalized_industries(industry)),
        is_st=is_st,
        is_new=is_new,
        min_score=min_score,
        max_score=max_score,
        min_trend_score=min_trend_score,
        max_trend_score=max_trend_score,
        min_change_pct=min_change_pct,
        max_change_pct=max_change_pct,
        min_turnover_rate=min_turnover_rate,
        max_turnover_rate=max_turnover_rate,
        min_amount=min_amount,
        max_amount=max_amount,
        min_data_quality_score=min_data_quality_score,
        max_data_quality_score=max_data_quality_score,
        keyword=keyword,
        sort=sorts,
        order=orders,
    ).normalized()


def _validate_filter_lists(
    market: list[MarketCode] | None,
    industry: list[str] | None,
    sort: list[MarketScanSort] | None,
    order: list[MarketScanSortOrder] | None,
) -> None:
    _validate_market_filter(market)
    _validate_industry_filter(industry)
    _validate_sort_filter(sort, order)


def _validate_market_filter(market: list[MarketCode] | None) -> None:
    if market and len(market) > 3:
        raise HTTPException(status_code=422, detail="市场条件最多 3 个且不能重复")
    if market and len(set(market)) != len(market):
        raise HTTPException(status_code=422, detail="市场条件最多 3 个且不能重复")


def _validate_industry_filter(industry: list[str] | None) -> None:
    normalized_industries = _normalized_industries(industry)
    if len(normalized_industries) > 20:
        raise HTTPException(status_code=422, detail="行业条件最多 20 个且不能重复")
    if len(set(normalized_industries)) != len(normalized_industries):
        raise HTTPException(status_code=422, detail="行业条件最多 20 个且不能重复")
    if any(_invalid_industry(value) for value in normalized_industries):
        raise HTTPException(status_code=422, detail="行业条件格式无效")


def _invalid_industry(value: str) -> bool:
    return len(value) > 80 or any(ord(character) < 32 for character in value)


def _validate_sort_filter(
    sort: list[MarketScanSort] | None,
    order: list[MarketScanSortOrder] | None,
) -> None:
    if sort and len(sort) > 3:
        raise HTTPException(status_code=422, detail="排序最多 3 级且字段不能重复")
    if sort and len(set(sort)) != len(sort):
        raise HTTPException(status_code=422, detail="排序最多 3 级且字段不能重复")
    if order is not None and len(order) != len(sort or ("rank",)):
        raise HTTPException(status_code=422, detail="排序字段与方向必须一一对应")


def _validate_filter_ranges(*ranges: tuple[str, int | float | None, int | float | None]) -> None:
    for label, minimum, maximum in ranges:
        if minimum is not None and maximum is not None and minimum > maximum:
            raise HTTPException(status_code=422, detail=f"{label}范围下限不能大于上限")


def _normalized_industries(values: list[str] | None) -> list[str]:
    return [" ".join(value.split()).strip() for value in values or () if " ".join(value.split()).strip()]


def _default_sort_order(field: MarketScanSort) -> MarketScanSortOrder:
    return "asc" if field in {"rank", "symbol"} else "desc"


@router.get("/api/market-scans/{run_id}/results", response_model=MarketScanResultPage)
async def market_scan_results(
    run_id: int,
    response: Response,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=200),
    filters: MarketScanExportFilters = Depends(market_scan_filter_query),
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> MarketScanResultPage:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: scanner.results(
            run_id,
            page=page,
            page_size=page_size,
            status=filters.status,
            market=filters.market,
            industry=filters.industry,
            is_st=filters.is_st,
            is_new=filters.is_new,
            min_score=filters.min_score,
            max_score=filters.max_score,
            min_trend_score=filters.min_trend_score,
            max_trend_score=filters.max_trend_score,
            min_change_pct=filters.min_change_pct,
            max_change_pct=filters.max_change_pct,
            min_turnover_rate=filters.min_turnover_rate,
            max_turnover_rate=filters.max_turnover_rate,
            min_amount=filters.min_amount,
            max_amount=filters.max_amount,
            min_data_quality_score=filters.min_data_quality_score,
            max_data_quality_score=filters.max_data_quality_score,
            keyword=filters.keyword,
            sort=filters.sort,
            order=filters.order,
        )
    )


@router.get(
    "/api/market-scans/{run_id}/export.xlsx",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "当前筛选结果的 Excel 工作簿"}},
)
async def export_market_scan_results(
    run_id: int,
    filters: MarketScanExportFilters = Depends(market_scan_filter_query),
    scanner: MarketScanManager = Depends(get_market_scanner),
) -> Response:
    exported = await run_sync_api_async(lambda: scanner.export_results(run_id, filters=filters))
    return Response(
        content=exported.content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{exported.filename}"',
            "X-Content-Type-Options": "nosniff",
        },
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
