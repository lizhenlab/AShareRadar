from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_datahub
from app.api.errors import run_api
from app.models.schemas import WatchlistScanRequest, WatchlistScanResponse
from app.services.datahub import DataHub
from app.services.watchlist_scan import scan_watchlist_conditions


router = APIRouter()


@router.post("/api/watchlist/scan", response_model=WatchlistScanResponse)
async def scan_watchlist(
    payload: WatchlistScanRequest,
    datahub: DataHub = Depends(get_datahub),
) -> WatchlistScanResponse:
    async def scan() -> WatchlistScanResponse:
        return await scan_watchlist_conditions(datahub, payload)

    return await run_api(scan)
