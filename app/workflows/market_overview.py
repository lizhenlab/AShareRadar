from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.config import Settings
from app.models.schemas import Kline, MarketOverview, Quote
from app.services.analysis import build_strong_stock_watch
from app.services.datahub import DataHub
from app.services.datahub_runtime import run_cache_io, run_cache_io_best_effort
from app.services.market_sampling import (
    QuoteSampleResult,
    STRONG_STOCK_SAMPLE_LIMIT,
    fetch_quote_sample,
    market_breadth_symbols as _market_breadth_symbols,
    unique_standard_symbols as _unique_standard_symbols,
)
from app.workflows.optional_data import short_error

MARKET_INDEX_SYMBOLS = ("sh000001", "sz399001", "sz399006")
CUSTOM_STRONG_STOCK_LIMIT = 50


@dataclass(frozen=True)
class KlineSampleResult:
    rows_by_code: dict[str, list[Kline]]
    failed_symbols: tuple[str, ...]


async def strong_stock_watch(datahub: DataHub, settings: Settings, symbols: str | None = None) -> dict[str, object]:
    is_custom_scope = symbols is not None
    if symbols is not None:
        raw_symbols = [item.strip() for item in symbols.split(",") if item.strip()]
        symbol_list = _unique_standard_symbols(raw_symbols)
        if not symbol_list:
            raise ValueError("至少输入一个有效股票代码")
        if len(symbol_list) > CUSTOM_STRONG_STOCK_LIMIT:
            raise ValueError(f"一次最多分析 {CUSTOM_STRONG_STOCK_LIMIT} 个股票代码")
        scope = "自定义列表"
    else:
        watch_symbols, watchlist_warning = await _watchlist_symbols_or_empty(datahub)
        breadth_symbols = await _market_breadth_symbols(datahub)
        symbol_list = _unique_standard_symbols([*watch_symbols, *settings.seed_symbols, *breadth_symbols])[
            :STRONG_STOCK_SAMPLE_LIMIT
        ]
        scope = "默认观察池 + 股票池分层抽样" if watchlist_warning else "自选股 + 默认观察池 + 股票池分层抽样"
        if not symbol_list:
            symbol_list = list(settings.seed_symbols)
            scope = "默认观察池"
    if is_custom_scope:
        watchlist_warning = ""
    quote_sample = await fetch_quote_sample(datahub, symbol_list, context=f"{scope}强股样本")
    quotes_data = list(quote_sample.quotes)
    _ensure_custom_quotes_available(is_custom_scope, symbol_list, quotes_data)
    kline_sample = await _kline_map_for_quotes(datahub, quotes_data, limit=80, context="强股观察池")
    items = build_strong_stock_watch(quotes_data, kline_sample.rows_by_code)
    status = _quote_sample_status(scope, quote_sample)
    warnings = _sample_warnings(
        status,
        [*_kline_sample_warnings(kline_sample, len(quotes_data)), *([watchlist_warning] if watchlist_warning else [])],
    )
    return {
        "updated_at": quotes_data[0].timestamp if quotes_data else "",
        "items": items,
        **status,
        "degraded": bool(warnings),
        "warnings": warnings,
    }


async def market_overview(datahub: DataHub, settings: Settings) -> MarketOverview:
    stock_symbols = list(settings.seed_symbols)
    index_sample, stock_sample = await asyncio.gather(
        fetch_quote_sample(datahub, MARKET_INDEX_SYMBOLS, context="市场指数样本"),
        fetch_quote_sample(datahub, stock_symbols, context="市场概览强股样本"),
    )
    index_quotes = list(index_sample.quotes)
    stock_quotes = list(stock_sample.quotes)
    kline_sample = await _kline_map_for_quotes(datahub, stock_quotes, limit=80, context="市场概览强股样本")
    strong = build_strong_stock_watch(stock_quotes, kline_sample.rows_by_code)
    index_meta = _quote_sample_status("市场指数样本", index_sample)
    strong_meta = _quote_sample_status("市场概览强股样本", stock_sample)
    strong_meta["warnings"] = _sample_warnings(
        strong_meta,
        _kline_sample_warnings(kline_sample, len(stock_quotes)),
    )
    strong_meta["degraded"] = bool(strong_meta["warnings"])
    warnings = _sample_warnings(index_meta, strong_meta["warnings"])
    return MarketOverview(
        indices=index_quotes,
        strong_stocks=strong[:5],
        index_meta=index_meta,
        strong_stocks_meta=strong_meta,
        degraded=bool(warnings),
        warnings=warnings,
        risk_note="本平台只用于个股研究和建议辅助，不做组合策略、不自动交易；实盘需结合个人仓位和风险承受能力。",
    )


async def _kline_map_for_quotes(datahub, quotes: list[Quote], *, limit: int, context: str) -> KlineSampleResult:
    symbols = [f"{item.code}.{item.market}" for item in quotes]
    rows = await asyncio.gather(*(datahub.kline(symbol, limit) for symbol in symbols), return_exceptions=True)
    result: dict[str, list[Kline]] = {}
    failed_symbols: list[str] = []
    for quote, symbol, item in zip(quotes, symbols, rows):
        if isinstance(item, BaseException):
            if isinstance(item, asyncio.CancelledError):
                raise item
            if not isinstance(item, Exception):
                raise item
            await _log_workflow_event(datahub, "fallback", f"{context}K线失败，{symbol} 不参与强股排序：{short_error(item)}")
            result[quote.code] = []
            failed_symbols.append(symbol)
            continue
        result[quote.code] = item
    return KlineSampleResult(rows_by_code=result, failed_symbols=tuple(failed_symbols))


def _quote_sample_status(scope: str, sample: QuoteSampleResult) -> dict[str, object]:
    warnings: list[str] = []
    if sample.requested_count == 0:
        warnings.append(f"{scope}未配置有效样本代码。")
    elif sample.unavailable:
        warnings.append(f"{scope}行情暂不可用，成功 0/{sample.requested_count} 个样本，请稍后重试。")
    elif sample.degraded:
        warnings.append(f"{scope}行情部分缺失，成功 {sample.sample_count}/{sample.requested_count} 个样本。")
    return {
        "scope": scope,
        "requested_count": sample.requested_count,
        "sample_count": sample.sample_count,
        "missing_count": len(sample.missing_symbols),
        "degraded": bool(warnings),
        "warnings": warnings,
    }


def _kline_sample_warnings(sample: KlineSampleResult, quote_count: int) -> list[str]:
    failed_count = len(sample.failed_symbols)
    if not failed_count:
        return []
    if quote_count and failed_count == quote_count:
        return ["强股排序所需日K线全部不可用，当前无法生成排序。"]
    return [f"强股排序已跳过 {failed_count} 只日K线不可用的股票。"]


def _sample_warnings(status: dict[str, object], extra: object) -> list[str]:
    base = status.get("warnings", [])
    values = [*base] if isinstance(base, list) else []
    if isinstance(extra, list):
        values.extend(item for item in extra if isinstance(item, str) and item.strip())
    return list(dict.fromkeys(values))[:5]


def _ensure_custom_quotes_available(is_custom_scope: bool, symbols: list[str], quotes: list[Quote]) -> None:
    if is_custom_scope and symbols and not quotes:
        raise RuntimeError(f"自定义强股列表行情不可用：{_format_symbols(symbols)}")


async def _watchlist_symbols_or_empty(datahub) -> tuple[list[str], str]:
    try:
        return await run_cache_io(datahub.cache.watchlist_symbols), ""
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        warning = f"自选股读取暂不可用，已使用默认观察池和股票池样本：{short_error(exc)}"
        await _log_workflow_event(datahub, "fallback", warning)
        return [], warning


async def _log_workflow_event(datahub, category: str, message: str) -> None:
    cache = getattr(datahub, "cache", None)
    log_event = getattr(cache, "log_event", None)
    if callable(log_event):
        await run_cache_io_best_effort(log_event, category, message)


def _format_symbols(symbols: list[str], limit: int = 5) -> str:
    shown = "、".join(symbols[:limit])
    suffix = f" 等 {len(symbols)} 个" if len(symbols) > limit else ""
    return f"{shown}{suffix}"


__all__ = ["market_overview", "strong_stock_watch"]
