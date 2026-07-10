from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_datahub
from app.api.errors import run_api, run_sync_api
from app.models.schemas import ChartMarkSummary, MutationResult, StockNoteInput, StockNoteItem, StockNoteUpdate
from app.services.chart_marks import build_chart_marks
from app.services.datahub import DataHub
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

    return run_sync_api(load)


@router.post("/api/stock/notes", response_model=StockNoteItem)
async def create_stock_note(
    payload: StockNoteInput,
    datahub: DataHub = Depends(get_datahub),
) -> StockNoteItem:
    async def create() -> StockNoteItem:
        normalize_symbol(payload.symbol)
        quote = await datahub.quote(payload.symbol)
        price = payload.price if payload.price is not None else quote.price
        raw_trade_date = payload.trade_date.strip() if payload.trade_date else ""
        trade_date = raw_trade_date or quote.timestamp
        enriched = StockNoteInput(
            symbol=payload.symbol,
            content=payload.content,
            note_type=payload.note_type,
            price=price,
            trade_date=trade_date,
            color=payload.color,
            visible=payload.visible,
        )
        return datahub.cache.create_stock_note(quote, enriched)

    return await run_api(create)


@router.delete("/api/stock/notes/{note_id}", response_model=MutationResult)
async def delete_stock_note(note_id: int, datahub: DataHub = Depends(get_datahub)) -> MutationResult:
    def remove() -> MutationResult:
        removed = datahub.cache.delete_stock_note(note_id)
        if not removed:
            raise HTTPException(status_code=404, detail="个股笔记不存在")
        return MutationResult(ok=True, removed=removed)

    return run_sync_api(remove)


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

    return run_sync_api(update)


@router.get("/api/stock/chart-marks", response_model=ChartMarkSummary)
async def chart_marks(
    symbol: str = Query("600519", description="6位A股代码"),
    limit: int = Query(80, ge=1, le=300),
    datahub: DataHub = Depends(get_datahub),
) -> ChartMarkSummary:
    return await run_api(lambda: build_chart_marks(datahub, symbol, limit=limit))
