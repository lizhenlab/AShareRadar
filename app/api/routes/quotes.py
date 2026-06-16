from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_app_settings, get_datahub
from app.api.errors import run_api
from app.config import Settings
from app.models.schemas import Quote
from app.services.datahub import DataHub
from app.utils.symbols import normalize_symbol


router = APIRouter()


@router.get("/api/quote", response_model=Quote)
async def quote(
    symbol: str = Query("600519", description="6位A股代码"),
    datahub: DataHub = Depends(get_datahub),
) -> Quote:
    async def load() -> Quote:
        normalize_symbol(symbol)
        return await datahub.quote(symbol)

    return await run_api(load)


@router.get("/api/quotes", response_model=list[Quote])
async def quotes(
    symbols: str = Query("600519,000001,300750"),
    datahub: DataHub = Depends(get_datahub),
) -> list[Quote]:
    symbol_list = [item.strip() for item in symbols.split(",") if item.strip()]

    async def load() -> list[Quote]:
        for symbol in symbol_list:
            normalize_symbol(symbol)
        return await datahub.quotes(symbol_list)

    return await run_api(load)


@router.get("/api/stream/quotes")
async def stream_quotes(
    request: Request,
    symbols: str = Query("600519,000001,300750"),
    datahub: DataHub = Depends(get_datahub),
    settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    requested_symbols = [item.strip() for item in symbols.split(",") if item.strip()]
    symbol_list = requested_symbols or datahub.cache.watchlist_symbols() or list(settings.seed_symbols[:5])
    try:
        for symbol in symbol_list:
            normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def event_generator():
        try:
            while not await request.is_disconnected():
                try:
                    data = [item.model_dump() for item in await datahub.quotes(symbol_list)]
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except Exception as exc:
                    yield f"event: quote-error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(settings.quote_refresh_seconds)
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_generator(), media_type="text/event-stream")
