from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_datahub
from app.api.errors import run_api, run_sync_api
from app.models.schemas import AdviceHistoryItem, WatchlistInput, WatchlistItem
from app.services.datahub import DataHub
from app.utils.symbols import normalize_symbol


router = APIRouter()


@router.get("/api/watchlist", response_model=list[WatchlistItem])
async def watchlist(datahub: DataHub = Depends(get_datahub)) -> list[WatchlistItem]:
    return run_sync_api(datahub.cache.watchlist)


@router.post("/api/watchlist", response_model=WatchlistItem)
async def add_watchlist_item(
    payload: WatchlistInput,
    datahub: DataHub = Depends(get_datahub),
) -> WatchlistItem:
    async def save() -> WatchlistItem:
        normalize_symbol(payload.symbol)
        quote_data = await datahub.quote(payload.symbol)
        return datahub.cache.save_watchlist_item(
            quote_data,
            note=payload.note,
            group_name=payload.group_name,
            pinned=payload.pinned,
        )

    return await run_api(save)


@router.delete("/api/watchlist/{symbol}")
async def delete_watchlist_item(symbol: str, datahub: DataHub = Depends(get_datahub)) -> dict[str, object]:
    def remove() -> dict[str, object]:
        normalize_symbol(symbol)
        removed = datahub.cache.delete_watchlist_item(symbol)
        if not removed:
            raise HTTPException(status_code=404, detail="自选股不存在")
        return {"ok": True, "removed": removed}

    return run_sync_api(remove)


@router.get("/api/advice/history", response_model=list[AdviceHistoryItem])
async def advice_history(
    symbol: str = Query("600519", description="6位A股代码"),
    limit: int = Query(30, ge=1, le=200),
    datahub: DataHub = Depends(get_datahub),
) -> list[AdviceHistoryItem]:
    def load() -> list[AdviceHistoryItem]:
        normalize_symbol(symbol)
        return datahub.cache.advice_history(symbol, limit=limit)

    return run_sync_api(load)
