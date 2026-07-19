from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.api.deps import get_app_settings, get_datahub
from app.api.errors import run_api, run_sync_api, run_sync_api_async
from app.config import Settings
from app.models.schemas import Quote
from app.services.datahub import DataHub
from app.services.datahub_status import _provider_error_text
from app.services.provider_errors import sanitize_provider_error
from app.utils.symbols import normalize_symbol, standard_symbol_list


router = APIRouter()
MAX_QUOTE_SYMBOLS = 50
MIN_QUOTE_REFRESH_SECONDS = 1


class _QuoteStreamResponse(StreamingResponse):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            # Disconnect and graceful-shutdown cancellation are normal for SSE.
            return


class _NoActiveWatchlistSymbolsError(ValueError):
    pass


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
    async def load() -> list[Quote]:
        symbol_list = _quote_symbol_list(symbols)
        return await datahub.quotes(symbol_list)

    return await run_api(load)


@router.get(
    "/api/stream/quotes",
    response_class=_QuoteStreamResponse,
    responses={
        200: {
            "description": "实时行情事件流",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def stream_quotes(
    request: Request,
    symbols: str = Query("600519,000001,300750"),
    datahub: DataHub = Depends(get_datahub),
    settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    symbol_list = run_sync_api(lambda: _requested_stream_symbol_list(symbols))
    if symbol_list is None:
        symbol_list = await run_sync_api_async(lambda: _api_stream_fallback_symbol_list(datahub, settings))
    return _QuoteStreamResponse(
        _quote_stream_events(request, datahub, symbol_list, settings.quote_refresh_seconds),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _stream_symbol_list(symbols: str, datahub: DataHub, settings: Settings) -> list[str]:
    requested_symbols = _requested_stream_symbol_list(symbols)
    if requested_symbols is not None:
        return requested_symbols
    return _stream_fallback_symbol_list(datahub, settings)


def _requested_stream_symbol_list(symbols: str) -> list[str] | None:
    requested_symbols = _split_symbol_query(symbols)
    return _bounded_symbol_list(requested_symbols) if requested_symbols else None


def _stream_fallback_symbol_list(datahub: DataHub, settings: Settings) -> list[str]:
    selection_reader = getattr(datahub.cache, "watchlist_symbol_selection", None)
    if not callable(selection_reader):
        watchlist_symbols = _fallback_symbol_list(datahub.cache.watchlist_symbols())
        if watchlist_symbols:
            return watchlist_symbols
        seed_symbols = _fallback_symbol_list(settings.seed_symbols[:5])
        if seed_symbols:
            return seed_symbols
        raise ValueError("至少输入一个股票代码")

    selection = selection_reader()
    active_symbols = _fallback_symbol_list(selection.active_symbols)
    if active_symbols:
        return active_symbols
    if selection.has_entries:
        raise _NoActiveWatchlistSymbolsError("观察池没有可订阅的活跃股票，请显式输入股票代码")
    excluded_symbols = set(_fallback_symbol_list(selection.excluded_symbols))
    seed_symbols = [
        symbol
        for symbol in _fallback_symbol_list(settings.seed_symbols)
        if symbol not in excluded_symbols
    ][:5]
    if seed_symbols:
        return seed_symbols
    raise ValueError("至少输入一个股票代码")


def _api_stream_fallback_symbol_list(datahub: DataHub, settings: Settings) -> list[str]:
    try:
        return _stream_fallback_symbol_list(datahub, settings)
    except _NoActiveWatchlistSymbolsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _quote_symbol_list(symbols: str) -> list[str]:
    symbol_list = _split_symbol_query(symbols)
    if not symbol_list:
        raise ValueError("至少输入一个股票代码")
    return _bounded_symbol_list(symbol_list)


def _split_symbol_query(symbols: str) -> list[str]:
    return [item.strip() for item in symbols.split(",") if item.strip()]


def _bounded_symbol_list(symbols: Iterable[str], *, truncate: bool = False, skip_invalid: bool = False) -> list[str]:
    return standard_symbol_list(
        symbols,
        skip_invalid=skip_invalid,
        max_items=MAX_QUOTE_SYMBOLS,
        truncate=truncate,
    ).symbols


def _fallback_symbol_list(symbols: Iterable[str]) -> list[str]:
    return _bounded_symbol_list(symbols, truncate=True, skip_invalid=True)


def _quote_refresh_interval(refresh_seconds: int | float) -> float:
    try:
        interval = float(refresh_seconds)
    except (TypeError, ValueError):
        return float(MIN_QUOTE_REFRESH_SECONDS)
    if not math.isfinite(interval):
        return float(MIN_QUOTE_REFRESH_SECONDS)
    return max(float(MIN_QUOTE_REFRESH_SECONDS), interval)


async def _quote_stream_events(request: Request, datahub: DataHub, symbol_list: list[str], refresh_seconds: int):
    refresh_interval = _quote_refresh_interval(refresh_seconds)
    try:
        while not await request.is_disconnected():
            yield await _next_quote_stream_event(datahub, symbol_list)
            await asyncio.sleep(refresh_interval)
    except asyncio.CancelledError:
        return


async def _next_quote_stream_event(datahub: DataHub, symbol_list: list[str]) -> str:
    try:
        data = [item.model_dump() for item in await datahub.quotes(symbol_list)]
        return _sse_message(data)
    except Exception as exc:
        return _sse_message(
            {"message": sanitize_provider_error(_provider_error_text(exc))},
            event="quote-error",
        )


def _sse_message(data: object, event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(_json_safe(data), ensure_ascii=False, default=str, allow_nan=False)}\n\n"


def _json_safe(value: object) -> object:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
