from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass

from app.models.schemas import Quote, StockInfo
from app.services.datahub_runtime import run_cache_io_best_effort
from app.services.provider_errors import sanitize_provider_error
from app.utils.symbols import standard_symbol_list


MARKET_BREADTH_LIMIT = 60
MARKET_BREADTH_BATCH_SIZE = 15
PEER_QUOTE_LIMIT = 18
STRONG_STOCK_SAMPLE_LIMIT = 30
MARKET_SAMPLE_WEIGHT = 0.55
INDUSTRY_SAMPLE_WEIGHT = 0.45
SAMPLE_MARKETS = {"SH", "SZ"}
INVALID_SAMPLE_TEXT_VALUES = {"", "none", "null", "nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}


@dataclass(frozen=True)
class MarketSampleGroups:
    by_market: dict[str, list[str]]
    by_industry: dict[str, list[str]]


@dataclass(frozen=True)
class MarketSamplingQuota:
    market_per_board: int
    industry_per_group: int


@dataclass(frozen=True)
class QuoteSampleResult:
    requested_symbols: tuple[str, ...]
    quotes: tuple[Quote, ...]
    missing_symbols: tuple[str, ...]
    fallback_batch_count: int = 0

    @property
    def requested_count(self) -> int:
        return len(self.requested_symbols)

    @property
    def sample_count(self) -> int:
        return len(self.quotes)

    @property
    def degraded(self) -> bool:
        return bool(self.missing_symbols)

    @property
    def unavailable(self) -> bool:
        return bool(self.requested_symbols) and not self.quotes


@dataclass(frozen=True)
class MarketBreadthSymbolResult:
    symbols: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketBreadthQuoteResult:
    quote_sample: QuoteSampleResult
    warnings: tuple[str, ...] = ()

    @property
    def quotes(self) -> tuple[Quote, ...]:
        return self.quote_sample.quotes


@dataclass(frozen=True)
class PeerQuoteSampleResult:
    quotes: tuple[Quote, ...] = ()
    status: str = "not_requested"
    requested_count: int = 0
    missing_count: int = 0
    warning: str | None = None


async def market_breadth_quotes(datahub) -> list[Quote]:
    return list((await market_breadth_quote_sample(datahub)).quotes)


async def market_breadth_quote_sample(datahub) -> MarketBreadthQuoteResult:
    symbol_sample = await _market_breadth_symbol_sample(datahub)
    if not symbol_sample.symbols:
        empty_sample = QuoteSampleResult(requested_symbols=(), quotes=(), missing_symbols=())
        warnings = _dedupe_strings([*symbol_sample.warnings, "市场宽度未配置有效样本代码。"])
        return MarketBreadthQuoteResult(quote_sample=empty_sample, warnings=tuple(warnings))
    quote_sample = await fetch_quote_sample(datahub, symbol_sample.symbols, context="市场宽度样本")
    warnings = list(symbol_sample.warnings)
    if quote_sample.unavailable:
        warnings.append("市场宽度行情样本暂不可用，环境判断已降级。")
    elif quote_sample.degraded:
        warnings.append(f"市场宽度行情样本部分缺失，成功 {quote_sample.sample_count}/{quote_sample.requested_count} 个。")
    return MarketBreadthQuoteResult(quote_sample=quote_sample, warnings=tuple(_dedupe_strings(warnings)))


async def market_breadth_symbols(datahub) -> list[str]:
    return list((await _market_breadth_symbol_sample(datahub)).symbols)


async def _market_breadth_symbol_sample(datahub) -> MarketBreadthSymbolResult:
    pool_result = await _stock_pool_sample(datahub, failure_message="市场宽度股票池不可用，仅使用种子样本")
    seed_symbols = unique_standard_symbols(datahub.settings.seed_symbols)
    pool_symbols = stratified_market_breadth_symbols(
        list(pool_result.rows),
        max(0, MARKET_BREADTH_LIMIT - len(seed_symbols)),
        seed_symbols,
    )
    symbols = list(dict.fromkeys([*seed_symbols, *pool_symbols]))[:MARKET_BREADTH_LIMIT]
    warnings = ("市场宽度股票池暂不可用，当前仅使用默认观察样本。",) if pool_result.unavailable else ()
    return MarketBreadthSymbolResult(symbols=tuple(symbols), warnings=warnings)


def stratified_market_breadth_symbols(
    pool: list[StockInfo],
    limit: int,
    seed_symbols: list[str] | None = None,
) -> list[str]:
    if limit <= 0:
        return []
    groups = _market_sample_groups(pool, _seed_codes(seed_symbols or []))
    quota = _market_sampling_quota(limit, groups)
    picked = _market_group_sample(groups.by_market, quota.market_per_board)
    picked.extend(_industry_group_sample(groups.by_industry, quota.industry_per_group))
    return _fill_sample_to_limit(picked, _all_market_symbols(groups), limit)


def industry_symbol_groups(pool: list[StockInfo], exclude_codes: set[str] | None = None) -> dict[str, list[str]]:
    exclude_codes = exclude_codes or set()
    grouped: dict[str, list[str]] = {}
    for item in pool:
        symbol = _sample_stock_symbol(item, exclude_codes)
        industry = _clean_sample_text(getattr(item, "industry", None))
        if not symbol or not industry:
            continue
        grouped.setdefault(industry, []).append(symbol)
    return {name: sorted(set(symbols)) for name, symbols in sorted(grouped.items())[:10]}


def _seed_codes(symbols: Iterable[str]) -> set[str]:
    codes: set[str] = set()
    for symbol in symbols:
        normalized = _standard_symbol_or_none(symbol)
        if normalized:
            codes.add(normalized.split(".")[0])
    return codes


def _market_sample_groups(pool: list[StockInfo], exclude_codes: set[str]) -> MarketSampleGroups:
    return MarketSampleGroups(
        by_market={market: _market_symbols(pool, market, exclude_codes) for market in sorted(SAMPLE_MARKETS)},
        by_industry=industry_symbol_groups(pool, exclude_codes=exclude_codes),
    )


def _market_symbols(pool: list[StockInfo], market: str, exclude_codes: set[str]) -> list[str]:
    symbols: set[str] = set()
    for item in pool:
        symbol = _sample_stock_symbol(item, exclude_codes)
        if symbol and _market_or_none(getattr(item, "market", None)) == market:
            symbols.add(symbol)
    return sorted(symbols)


def _sample_stock_symbol(item: StockInfo, exclude_codes: set[str]) -> str | None:
    code = _stock_code_or_none(getattr(item, "code", None))
    market = _market_or_none(getattr(item, "market", None))
    if not code or not market or code in exclude_codes:
        return None
    normalized = _standard_symbol_or_none(item.symbol)
    if not normalized:
        return None
    normalized_code, normalized_market = normalized.split(".")
    if normalized_code != code or normalized_market != market:
        return None
    return normalized


def _market_sampling_quota(limit: int, groups: MarketSampleGroups) -> MarketSamplingQuota:
    market_group_count = max(1, len([symbols for symbols in groups.by_market.values() if symbols]))
    industry_group_count = max(1, len(groups.by_industry))
    return MarketSamplingQuota(
        market_per_board=max(1, round(limit * MARKET_SAMPLE_WEIGHT / market_group_count)),
        industry_per_group=max(1, round(limit * INDUSTRY_SAMPLE_WEIGHT / industry_group_count)),
    )


def _market_group_sample(groups: dict[str, list[str]], quota: int) -> list[str]:
    picked: list[str] = []
    for market in sorted(groups):
        picked.extend(even_sample(groups[market], quota))
    return picked


def _industry_group_sample(groups: dict[str, list[str]], quota: int) -> list[str]:
    picked: list[str] = []
    for symbols in groups.values():
        picked.extend(even_sample(symbols, quota))
    return picked


def _all_market_symbols(groups: MarketSampleGroups) -> list[str]:
    return sorted({symbol for symbols in groups.by_market.values() for symbol in symbols})


def _fill_sample_to_limit(picked: list[str], candidates: list[str], limit: int) -> list[str]:
    deduped = _dedupe_strings(picked)
    if len(deduped) < limit:
        remaining = sorted(set(candidates) - set(deduped))
        deduped.extend(even_sample(remaining, limit - len(deduped)))
    return _dedupe_strings(deduped)[:limit]


def even_sample(items: list[str], limit: int) -> list[str]:
    if limit <= 0 or not items:
        return []
    unique_items = _dedupe_strings(items)
    if len(unique_items) <= limit:
        return unique_items[:]
    picked = _dedupe_strings(unique_items[index] for index in _even_sample_indices(len(unique_items), limit))
    picked.extend(_dedupe_strings(item for item in unique_items if item not in picked)[: limit - len(picked)])
    return picked[:limit]


def _even_sample_indices(item_count: int, limit: int) -> list[int]:
    if limit == 1:
        return [item_count // 2]
    step = (item_count - 1) / (limit - 1)
    return [round(index * step) for index in range(limit)]


def _dedupe_strings(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def dedupe_quotes(quotes: Iterable[Quote]) -> list[Quote]:
    by_symbol: dict[str, Quote] = {}
    for quote in quotes:
        symbol = _quote_symbol_or_none(quote)
        if symbol:
            by_symbol[symbol] = quote
    return list(by_symbol.values())


async def peer_quotes(datahub, profile: StockInfo | None, target_symbol: str) -> list[Quote]:
    return list((await peer_quote_sample(datahub, profile, target_symbol)).quotes)


async def peer_quote_sample(datahub, profile: StockInfo | None, target_symbol: str) -> PeerQuoteSampleResult:
    industry = _clean_sample_text(getattr(profile, "industry", None)) if profile else None
    if not profile or not industry:
        return PeerQuoteSampleResult(
            status="not_applicable",
            warning="行业归属待确认，暂无法建立同行样本。",
        )
    pool_result = await _stock_pool_sample(datahub, failure_message=f"{industry}同行股票池不可用，同行样本为空")
    if pool_result.unavailable:
        return PeerQuoteSampleResult(
            status="unavailable",
            warning=f"{industry}同行股票池暂不可用。",
        )
    selected = peer_symbols(list(pool_result.rows), profile, target_symbol, PEER_QUOTE_LIMIT)
    if not selected:
        return PeerQuoteSampleResult(status="insufficient")
    quote_sample = await fetch_quote_sample(datahub, selected, context=f"{industry}同行样本")
    if quote_sample.unavailable:
        status = "unavailable"
        warning = f"{industry}同行行情样本暂不可用。"
    elif quote_sample.degraded:
        status = "degraded"
        warning = f"{industry}同行行情样本部分缺失，成功 {quote_sample.sample_count}/{quote_sample.requested_count} 个。"
    else:
        status = "available"
        warning = None
    return PeerQuoteSampleResult(
        quotes=quote_sample.quotes,
        status=status,
        requested_count=quote_sample.requested_count,
        missing_count=len(quote_sample.missing_symbols),
        warning=warning,
    )


def peer_symbols(pool: list[StockInfo], profile: StockInfo | None, target_symbol: str, limit: int = PEER_QUOTE_LIMIT) -> list[str]:
    industry = _clean_sample_text(getattr(profile, "industry", None)) if profile else None
    if not profile or not industry:
        return []
    target_codes = _seed_codes([target_symbol])
    profile_code = _stock_code_or_none(getattr(profile, "code", None))
    if profile_code:
        target_codes.add(profile_code)
    peers: set[str] = set()
    for item in pool:
        if _clean_sample_text(getattr(item, "industry", None)) != industry:
            continue
        symbol = _sample_stock_symbol(item, target_codes)
        if symbol:
            peers.add(symbol)
    return even_sample(sorted(peers), limit)


async def fetch_quotes_with_single_fallback(
    datahub,
    symbols: Iterable[str],
    *,
    batch_size: int = MARKET_BREADTH_BATCH_SIZE,
    context: str = "行情样本",
) -> list[Quote]:
    result = await fetch_quote_sample(
        datahub,
        symbols,
        batch_size=batch_size,
        context=context,
    )
    return list(result.quotes)


async def fetch_quote_sample(
    datahub,
    symbols: Iterable[str],
    *,
    batch_size: int = MARKET_BREADTH_BATCH_SIZE,
    context: str = "行情样本",
) -> QuoteSampleResult:
    raw_symbols = list(symbols)
    normalized_symbols = unique_standard_symbols(raw_symbols)
    if len(normalized_symbols) < len(raw_symbols):
        await _log_sampling_event(
            datahub,
            "fallback",
            f"{context}剔除 {len(raw_symbols) - len(normalized_symbols)} 个重复或无效样本。",
        )
    batch_size = max(1, batch_size)
    if not normalized_symbols:
        return QuoteSampleResult(requested_symbols=(), quotes=(), missing_symbols=())
    quotes: list[Quote] = []
    fallback_batches = 0
    for batch in _symbol_batches(normalized_symbols, batch_size):
        batch_result = await _fetch_quote_batch_with_fallback(datahub, batch, context)
        quotes.extend(batch_result.quotes)
        fallback_batches += int(batch_result.fallback_used)
    ordered_quotes = _requested_quotes_in_order(quotes, normalized_symbols)
    returned_symbols = {_quote_symbol_or_none(quote) for quote in ordered_quotes}
    missing_symbols = [symbol for symbol in normalized_symbols if symbol not in returned_symbols]
    if missing_symbols:
        await _log_sampling_event(
            datahub,
            "fallback",
            f"{context}最终缺失 {len(missing_symbols)} / {len(normalized_symbols)} 个样本，触发逐只回退 {fallback_batches} 批。",
        )
    return QuoteSampleResult(
        requested_symbols=tuple(normalized_symbols),
        quotes=tuple(ordered_quotes),
        missing_symbols=tuple(missing_symbols),
        fallback_batch_count=fallback_batches,
    )


@dataclass(frozen=True)
class QuoteBatchResult:
    quotes: list[Quote]
    failed_symbols: list[str]
    fallback_used: bool


def _symbol_batches(symbols: list[str], batch_size: int) -> list[list[str]]:
    return [symbols[index : index + batch_size] for index in range(0, len(symbols), batch_size)]


async def _fetch_quote_batch_with_fallback(datahub, batch: list[str], context: str) -> QuoteBatchResult:
    quotes, exc = await _quotes_or_error(datahub, batch)
    if exc is None:
        matched_quotes, missing_symbols = _match_requested_quotes(quotes, batch)
        if not missing_symbols:
            return QuoteBatchResult(quotes=matched_quotes, failed_symbols=[], fallback_used=False)
        await _log_sampling_event(
            datahub,
            "fallback",
            f"{context}批量行情缺失 {len(missing_symbols)} 个样本，改为逐只补齐：{_format_symbols(missing_symbols)}",
        )
        quotes, failed_symbols = await _fetch_single_quotes(datahub, missing_symbols, context)
        return QuoteBatchResult(
            quotes=[*matched_quotes, *quotes],
            failed_symbols=failed_symbols,
            fallback_used=True,
        )
    await _log_sampling_event(datahub, "fallback", f"{context}批量行情失败，改为逐只重试：{_short_error(exc)}")
    quotes, failed_symbols = await _fetch_single_quotes(datahub, batch, context)
    return QuoteBatchResult(quotes=quotes, failed_symbols=failed_symbols, fallback_used=True)


async def _fetch_single_quotes(datahub, symbols: list[str], context: str) -> tuple[list[Quote], list[str]]:
    quotes: list[Quote] = []
    failed_symbols: list[str] = []
    for symbol in symbols:
        single_quotes, exc = await _quotes_or_error(datahub, [symbol])
        if exc is None:
            matched_quotes, missing_symbols = _match_requested_quotes(single_quotes, [symbol])
            if matched_quotes:
                quotes.extend(matched_quotes)
                continue
            failed_symbols.extend(missing_symbols)
            await _log_sampling_event(datahub, "fallback", f"{context}单只行情未返回请求符号：{symbol}")
            continue
        failed_symbols.append(symbol)
        await _log_sampling_event(datahub, "fallback", f"{context}单只行情失败：{symbol}；{_short_error(exc)}")
    return quotes, failed_symbols


def unique_standard_symbols(symbols: Iterable[str]) -> list[str]:
    return standard_symbol_list(symbols, skip_invalid=True).symbols


async def _stock_pool_or_empty(datahub, *, failure_message: str) -> list[StockInfo]:
    return list((await _stock_pool_sample(datahub, failure_message=failure_message)).rows)


@dataclass(frozen=True)
class StockPoolSampleResult:
    rows: tuple[StockInfo, ...]
    unavailable: bool = False


async def _stock_pool_sample(datahub, *, failure_message: str) -> StockPoolSampleResult:
    # DataHub folds provider/cache failures into heterogeneous Exception subclasses.
    try:
        return StockPoolSampleResult(rows=tuple(await datahub.stock_pool(limit=1200, refresh=False)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await _log_sampling_event(datahub, "fallback", f"{failure_message}：{_short_error(exc)}")
        return StockPoolSampleResult(rows=(), unavailable=True)


async def _quotes_or_error(datahub, symbols: list[str]) -> tuple[list[Quote], Exception | None]:
    # Keep fallback swallowing at this boundary; BaseException control flow still propagates.
    try:
        return list(await datahub.quotes(symbols)), None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return [], exc


def _match_requested_quotes(quotes: Iterable[Quote], requested_symbols: list[str]) -> tuple[list[Quote], list[str]]:
    by_symbol = _quote_map_for_requested_symbols(quotes, set(requested_symbols))
    matched = [by_symbol[symbol] for symbol in requested_symbols if symbol in by_symbol]
    missing = [symbol for symbol in requested_symbols if symbol not in by_symbol]
    return matched, missing


def _requested_quotes_in_order(quotes: Iterable[Quote], requested_symbols: list[str]) -> list[Quote]:
    return _match_requested_quotes(quotes, requested_symbols)[0]


def _quote_map_for_requested_symbols(quotes: Iterable[Quote], requested_symbols: set[str]) -> dict[str, Quote]:
    by_symbol: dict[str, Quote] = {}
    for quote in quotes:
        symbol = _quote_symbol_or_none(quote)
        if symbol in requested_symbols:
            by_symbol[symbol] = quote
    return by_symbol


def _quote_symbol_or_none(quote: Quote) -> str | None:
    code = getattr(quote, "code", "")
    market = getattr(quote, "market", "")
    if not code or not market:
        return None
    return _standard_symbol_or_none(f"{code}.{market}")


def _standard_symbol_or_none(symbol: object) -> str | None:
    result = standard_symbol_list([symbol], skip_invalid=True)
    return result.symbols[0] if result.symbols else None


def _stock_code_or_none(value: object) -> str | None:
    text = _clean_sample_text(value)
    if not text or len(text) != 6 or not text.isdigit() or text == "000000":
        return None
    return text


def _market_or_none(value: object) -> str | None:
    text = _clean_sample_text(value)
    if not text:
        return None
    market = text.upper()
    return market if market in SAMPLE_MARKETS else None


def _clean_sample_text(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return None if text.casefold() in INVALID_SAMPLE_TEXT_VALUES else text


async def _log_sampling_event(datahub, category: str, message: str) -> None:
    cache = getattr(datahub, "cache", None)
    log_event = getattr(cache, "log_event", None)
    if callable(log_event):
        await run_cache_io_best_effort(log_event, category, message)


def _short_error(exc: Exception) -> str:
    text = sanitize_provider_error(exc).strip()
    return text[:140] if text else exc.__class__.__name__


def _format_symbols(symbols: list[str], limit: int = 5) -> str:
    shown = symbols[:limit]
    suffix = f" 等 {len(symbols)} 个" if len(symbols) > limit else ""
    return "、".join(shown) + suffix


__all__ = [
    "MARKET_BREADTH_BATCH_SIZE",
    "MARKET_BREADTH_LIMIT",
    "MarketBreadthQuoteResult",
    "MarketBreadthSymbolResult",
    "PEER_QUOTE_LIMIT",
    "PeerQuoteSampleResult",
    "QuoteSampleResult",
    "STRONG_STOCK_SAMPLE_LIMIT",
    "dedupe_quotes",
    "even_sample",
    "fetch_quotes_with_single_fallback",
    "fetch_quote_sample",
    "industry_symbol_groups",
    "market_breadth_quotes",
    "market_breadth_quote_sample",
    "market_breadth_symbols",
    "peer_quotes",
    "peer_quote_sample",
    "stratified_market_breadth_symbols",
    "unique_standard_symbols",
]
