from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_datahub
from app.api.errors import run_api, run_sync_api
from app.models.schemas import DataStatus, FutuStatusResponse, OrderBook, PlateItem, StockInfo, TradeCalendarRefreshResponse
from app.services.datahub import DataHub
from app.services.trading_calendar import TradeCalendarRefreshResult, refresh_trade_calendar_result
from app.utils.symbols import normalize_symbol


router = APIRouter()


@router.get("/api/data/status", response_model=DataStatus)
async def data_status(datahub: DataHub = Depends(get_datahub)) -> DataStatus:
    return run_sync_api(datahub.status)


@router.get("/api/stocks", response_model=list[StockInfo])
async def stocks(
    keyword: str | None = Query(None, description="股票代码或名称关键字"),
    limit: int = Query(50, ge=1, le=500),
    refresh: bool = Query(False, description="是否强制刷新股票池"),
    datahub: DataHub = Depends(get_datahub),
) -> list[StockInfo]:
    return await run_api(lambda: datahub.stock_pool(keyword=keyword, limit=limit, refresh=refresh))


@router.get("/api/plates", response_model=list[PlateItem])
async def plates(
    limit: int = Query(20, ge=1, le=100),
    refresh: bool = Query(False, description="是否强制刷新板块排行"),
    datahub: DataHub = Depends(get_datahub),
) -> list[PlateItem]:
    return await run_api(lambda: datahub.plate_rank(limit=limit, refresh=refresh))


@router.get("/api/order-book", response_model=OrderBook)
async def order_book(
    symbol: str = Query("600519", description="6位A股代码"),
    datahub: DataHub = Depends(get_datahub),
) -> OrderBook:
    async def load() -> OrderBook:
        normalize_symbol(symbol)
        return await datahub.order_book(symbol)

    return await run_api(load)


@router.get("/api/futu/status", response_model=FutuStatusResponse)
async def futu_status(datahub: DataHub = Depends(get_datahub)) -> FutuStatusResponse:
    return await run_api(datahub.futu_ping)


@router.post("/api/data/trading-calendar/refresh", response_model=TradeCalendarRefreshResponse, response_model_exclude_none=True)
async def refresh_trading_calendar_api() -> TradeCalendarRefreshResponse:
    async def refresh() -> TradeCalendarRefreshResponse:
        result = await asyncio.to_thread(refresh_trade_calendar_result)
        return _trade_calendar_refresh_payload(result)

    return await run_api(refresh)


def _trade_calendar_refresh_payload(result: TradeCalendarRefreshResult) -> TradeCalendarRefreshResponse:
    return TradeCalendarRefreshResponse(
        ok=result.ok,
        trade_date_count=result.trade_date_count,
        source=result.source,
        error=result.error or None,
    )
