from __future__ import annotations

import asyncio

from app.config import Settings
from app.models.schemas import Kline, MarketOverview, Quote
from app.services.analysis import build_strong_stock_watch
from app.services.datahub import DataHub
from app.services.market_sampling import (
    STRONG_STOCK_SAMPLE_LIMIT,
    fetch_quotes_with_single_fallback,
    market_breadth_symbols as _market_breadth_symbols,
    unique_standard_symbols as _unique_standard_symbols,
)

MARKET_INDEX_SYMBOLS = ("sh000001", "sz399001", "sz399006")
CUSTOM_STRONG_STOCK_LIMIT = 50


async def strong_stock_watch(datahub: DataHub, settings: Settings, symbols: str | None = None) -> dict[str, object]:
    if symbols:
        raw_symbols = [item.strip() for item in symbols.split(",") if item.strip()]
        symbol_list = _unique_standard_symbols(raw_symbols)
        if not symbol_list:
            raise ValueError("至少输入一个有效股票代码")
        if len(symbol_list) > CUSTOM_STRONG_STOCK_LIMIT:
            raise ValueError(f"一次最多分析 {CUSTOM_STRONG_STOCK_LIMIT} 个股票代码")
        scope = "自定义列表"
    else:
        watch_symbols = datahub.cache.watchlist_symbols()
        breadth_symbols = await _market_breadth_symbols(datahub)
        symbol_list = _unique_standard_symbols([*watch_symbols, *settings.seed_symbols, *breadth_symbols])[
            :STRONG_STOCK_SAMPLE_LIMIT
        ]
        scope = "自选股 + 默认观察池 + 股票池分层抽样"
        if not symbol_list:
            symbol_list = list(settings.seed_symbols)
            scope = "默认观察池"
    quotes_data = await fetch_quotes_with_single_fallback(datahub, symbol_list, context=f"{scope}强股样本")
    kline_map = await _kline_map_for_quotes(datahub, quotes_data, limit=80, context="强股观察池")
    items = build_strong_stock_watch(quotes_data, kline_map)
    return {"updated_at": quotes_data[0].timestamp if quotes_data else "", "items": items, "scope": scope, "sample_count": len(quotes_data)}


async def market_overview(datahub: DataHub, settings: Settings) -> MarketOverview:
    stock_symbols = list(settings.seed_symbols)
    index_quotes, stock_quotes = await asyncio.gather(
        fetch_quotes_with_single_fallback(datahub, MARKET_INDEX_SYMBOLS, context="市场指数样本"),
        fetch_quotes_with_single_fallback(datahub, stock_symbols, context="市场概览强股样本"),
    )
    kline_map = await _kline_map_for_quotes(datahub, stock_quotes, limit=80, context="市场概览强股样本")
    strong = build_strong_stock_watch(stock_quotes, kline_map)
    return MarketOverview(
        indices=index_quotes,
        strong_stocks=strong[:5],
        risk_note="本平台只用于个股研究和建议辅助，不做组合策略、不自动交易；实盘需结合个人仓位和风险承受能力。",
    )


async def _kline_map_for_quotes(datahub, quotes: list[Quote], *, limit: int, context: str) -> dict[str, list[Kline]]:
    symbols = [f"{item.code}.{item.market}" for item in quotes]
    rows = await asyncio.gather(*(datahub.kline(symbol, limit) for symbol in symbols), return_exceptions=True)
    result: dict[str, list[Kline]] = {}
    for quote, symbol, item in zip(quotes, symbols, rows):
        if isinstance(item, Exception):
            _log_workflow_event(datahub, "fallback", f"{context}K线失败，{symbol} 不参与强股排序：{_short_error(item)}")
            result[quote.code] = []
        else:
            result[quote.code] = item
    return result


def _log_workflow_event(datahub, category: str, message: str) -> None:
    cache = getattr(datahub, "cache", None)
    log_event = getattr(cache, "log_event", None)
    if callable(log_event):
        log_event(category, message)


def _short_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text[:140] if text else exc.__class__.__name__


__all__ = ["market_overview", "strong_stock_watch"]
