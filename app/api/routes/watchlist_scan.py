from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_datahub
from app.api.errors import run_api, run_sync_api_async
from app.models.reviews import (
    WatchlistScanRequest,
    WatchlistScanHistoryItem,
    WatchlistScanRecord,
)
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io
from app.services.watchlist_scan import scan_watchlist_conditions


router = APIRouter()


@router.post("/api/watchlist/scan", response_model=WatchlistScanRecord)
async def scan_watchlist(
    payload: WatchlistScanRequest,
    datahub: DataHub = Depends(get_datahub),
) -> WatchlistScanRecord:
    async def scan() -> WatchlistScanRecord:
        result = await scan_watchlist_conditions(datahub, payload)
        return await run_cache_io(datahub.cache.save_watchlist_scan, payload, result)

    return await run_api(scan)


@router.get("/api/watchlist/scans", response_model=list[WatchlistScanHistoryItem])
async def watchlist_scan_history(
    limit: int = Query(20, ge=1, le=100),
    datahub: DataHub = Depends(get_datahub),
) -> list[WatchlistScanHistoryItem]:
    return await run_sync_api_async(lambda: datahub.cache.watchlist_scan_history(limit=limit))


@router.get("/api/watchlist/scans/{scan_id}", response_model=WatchlistScanRecord)
async def watchlist_scan_record(
    scan_id: int,
    datahub: DataHub = Depends(get_datahub),
) -> WatchlistScanRecord:
    def load() -> WatchlistScanRecord:
        record = datahub.cache.watchlist_scan_record(scan_id)
        if record is None:
            raise HTTPException(status_code=404, detail="观察池扫描历史不存在")
        return record

    return await run_sync_api_async(load)
