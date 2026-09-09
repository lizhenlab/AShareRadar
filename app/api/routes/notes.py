from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import get_datahub
from app.api.errors import run_api, run_sync_api_async
from app.models.user_data import (
    ChartMarkSummary,
    StockNoteInput,
    StockNoteItem,
    StockNoteUpdate,
)
from app.models.system import (
    MutationResult,
)
from app.services.chart_marks import build_chart_marks
from app.services.datahub import DataHub
from app.services.stock_note_creation import prepare_stock_note_creation
from app.utils.symbols import normalize_symbol


router = APIRouter()


@router.get("/api/stock/notes", response_model=list[StockNoteItem])
async def stock_notes(
    symbol: str = Query("600519", description="6位A股代码"),
    limit: int = Query(100, ge=1, le=500),
    datahub: DataHub = Depends(get_datahub),
) -> list[StockNoteItem]:
    def load() -> list[StockNoteItem]:
        normalize_symbol(symbol)
        return datahub.cache.stock_notes(symbol, limit=limit)

    return await run_sync_api_async(load)


@router.post("/api/stock/notes", response_model=StockNoteItem)
async def create_stock_note(
    payload: StockNoteInput,
    datahub: DataHub = Depends(get_datahub),
) -> StockNoteItem:
    async def create() -> StockNoteItem:
        normalize_symbol(payload.symbol)
        stock, enriched = await prepare_stock_note_creation(datahub, payload)
        return await run_sync_api_async(lambda: datahub.cache.create_stock_note(stock, enriched))

    return await run_api(create)


@router.delete("/api/stock/notes/{note_id}", response_model=MutationResult)
async def delete_stock_note(
    note_id: int,
    expected_revision: str = Query(..., pattern=r"^[0-9a-f]{64}$"),
    datahub: DataHub = Depends(get_datahub),
) -> MutationResult:
    def remove() -> MutationResult:
        removed = datahub.cache.delete_stock_note(note_id, expected_revision=expected_revision)
        if not removed:
            raise HTTPException(status_code=404, detail="个股笔记不存在")
        return MutationResult(ok=True, removed=removed)

    return await run_sync_api_async(remove)


@router.get("/api/stock/notes/{note_id}", response_model=StockNoteItem)
async def stock_note(
    note_id: int, response: Response, datahub: DataHub = Depends(get_datahub),
) -> StockNoteItem:
    def load() -> StockNoteItem:
        item = datahub.cache.stock_note(note_id)
        if item is None:
            raise HTTPException(status_code=404, detail="个股笔记不存在")
        return item

    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(load)


@router.patch("/api/stock/notes/{note_id}", response_model=StockNoteItem)
async def update_stock_note(
    note_id: int,
    payload: StockNoteUpdate,
    datahub: DataHub = Depends(get_datahub),
) -> StockNoteItem:
    def update() -> StockNoteItem:
        note = datahub.cache.update_stock_note(note_id, payload)
        if note is None:
            raise HTTPException(status_code=404, detail="个股笔记不存在")
        return note

    return await run_sync_api_async(update)


@router.get("/api/stock/chart-marks", response_model=ChartMarkSummary)
async def chart_marks(
    symbol: str = Query("600519", description="6位A股代码"),
    limit: int = Query(80, ge=1, le=300),
    datahub: DataHub = Depends(get_datahub),
) -> ChartMarkSummary:
    return await run_api(lambda: build_chart_marks(datahub, symbol, limit=limit))
