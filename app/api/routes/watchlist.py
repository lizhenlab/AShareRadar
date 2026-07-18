from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_datahub
from app.api.errors import run_api, run_sync_api_async
from app.models.schemas import (
    AdviceHistoryItem,
    AdviceTimelineItem,
    MutationResult,
    WatchlistInput,
    WatchlistItem,
    WatchlistMarkViewed,
    WatchlistUpdate,
)
from app.services.datahub import DataHub
from app.utils.symbols import normalize_symbol


router = APIRouter()


@router.get("/api/watchlist", response_model=list[WatchlistItem])
async def watchlist(datahub: DataHub = Depends(get_datahub)) -> list[WatchlistItem]:
    return await run_sync_api_async(datahub.cache.watchlist)


@router.post("/api/watchlist", response_model=WatchlistItem)
async def add_watchlist_item(
    payload: WatchlistInput,
    datahub: DataHub = Depends(get_datahub),
) -> WatchlistItem:
    async def save() -> WatchlistItem:
        normalize_symbol(payload.symbol)
        quote_data = await datahub.quote(payload.symbol)
        return await run_sync_api_async(
            lambda: datahub.cache.save_watchlist_item(
                quote_data,
                note=payload.note,
                group_name=payload.group_name,
                pinned=payload.pinned,
                research_status=payload.research_status,
                priority=payload.priority,
                next_review_date=payload.next_review_date,
            )
        )

    return await run_api(save)


@router.patch("/api/watchlist/{symbol}", response_model=WatchlistItem)
async def update_watchlist_item(
    symbol: str,
    payload: WatchlistUpdate,
    datahub: DataHub = Depends(get_datahub),
) -> WatchlistItem:
    def update() -> WatchlistItem:
        normalize_symbol(symbol)
        item = datahub.cache.update_watchlist_item(symbol, payload)
        if item is None:
            raise HTTPException(status_code=404, detail="自选股不存在")
        return item

    return await run_sync_api_async(update)


@router.post("/api/watchlist/{symbol}/mark-viewed", response_model=WatchlistItem)
async def mark_watchlist_item_viewed(
    symbol: str,
    payload: WatchlistMarkViewed,
    datahub: DataHub = Depends(get_datahub),
) -> WatchlistItem:
    def mark_viewed() -> WatchlistItem:
        normalize_symbol(symbol)
        item = datahub.cache.mark_watchlist_viewed(
            symbol,
            clear_unread=payload.clear_unread,
            viewed_through_advice_id=payload.viewed_through_advice_id,
        )
        if item is None:
            raise HTTPException(status_code=404, detail="自选股不存在")
        return item

    return await run_sync_api_async(mark_viewed)


@router.delete("/api/watchlist/{symbol}", response_model=MutationResult)
async def delete_watchlist_item(symbol: str, datahub: DataHub = Depends(get_datahub)) -> MutationResult:
    def remove() -> MutationResult:
        normalize_symbol(symbol)
        removed = datahub.cache.delete_watchlist_item(symbol)
        if not removed:
            raise HTTPException(status_code=404, detail="自选股不存在")
        return MutationResult(ok=True, removed=removed)

    return await run_sync_api_async(remove)


@router.get("/api/advice/history", response_model=list[AdviceHistoryItem])
async def advice_history(
    symbol: str = Query("600519", description="6位A股代码"),
    limit: int = Query(30, ge=1, le=200),
    datahub: DataHub = Depends(get_datahub),
) -> list[AdviceHistoryItem]:
    def load() -> list[AdviceHistoryItem]:
        normalize_symbol(symbol)
        return datahub.cache.advice_history(symbol, limit=limit)

    return await run_sync_api_async(load)


@router.get("/api/advice/timeline", response_model=list[AdviceTimelineItem])
async def advice_timeline(
    symbol: str = Query("600519", description="6位A股代码"),
    limit: int = Query(30, ge=1, le=200),
    datahub: DataHub = Depends(get_datahub),
) -> list[AdviceTimelineItem]:
    def load() -> list[AdviceTimelineItem]:
        normalize_symbol(symbol)
        return datahub.cache.advice_timeline(symbol, limit=limit)

    return await run_sync_api_async(load)
