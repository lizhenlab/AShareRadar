"""HTTP binding for the current-watchlist read overlay."""
from collections.abc import Callable
from typing import Literal, TypeVar
from fastapi import HTTPException, Query, Request
from app.services.market_scan_export import MarketScanExportFilters, frozen_market_scan_export_filters
from app.models.market_scan_selection import MarketScanSelectionRequest
from app.repositories.market_scan_selection import MarketScanSelectionConflict

T = TypeVar("T")


def market_scan_selection_query(
    universe: Literal["all", "watchlist"] = Query("all"),
    watchlist_digest: str | None = Query(None, pattern=r"^[0-9a-f]{64}$"),
) -> MarketScanSelectionRequest:
    if universe == "all" and watchlist_digest is not None:
        raise HTTPException(status_code=422, detail="全市场范围不能绑定自选成员摘要")
    return MarketScanSelectionRequest(universe=universe, watchlist_digest=watchlist_digest)


def selection_guard(call: Callable[[], T]) -> T:
    try:
        return call()
    except MarketScanSelectionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc), headers={"Cache-Control": "no-store"}) from exc


def export_selection_filters(
    request: Request, filters: MarketScanExportFilters, ranking_basis: str, screen_spec: str | None,
) -> MarketScanExportFilters:
    if ranking_basis == "production":
        if screen_spec is not None:
            raise HTTPException(status_code=422, detail="完整筛选合同仅可用于冻结评分导出")
        return filters
    if screen_spec is None:
        raise HTTPException(status_code=422, detail="冻结评分导出必须提供完整筛选合同")
    if set(request.query_params) - {"ranking_basis", "screen_spec", "universe", "watchlist_digest"}:
        raise HTTPException(status_code=422, detail="完整筛选合同不能与普通筛选参数混用")
    if filters.min_upside_probability is not None:
        raise HTTPException(status_code=422, detail="冻结评分导出不能使用当前概率研究条件")
    try:
        return frozen_market_scan_export_filters(filters, screen_spec)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="冻结导出的完整筛选合同无效") from exc
