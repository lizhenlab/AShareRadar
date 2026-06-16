from __future__ import annotations

from collections.abc import Iterable

from app.models.schemas import Quote, StockInfo
from app.utils.symbols import standard_symbol


MARKET_BREADTH_LIMIT = 60
MARKET_BREADTH_BATCH_SIZE = 15
PEER_QUOTE_LIMIT = 18
STRONG_STOCK_SAMPLE_LIMIT = 30


async def market_breadth_quotes(datahub) -> list[Quote]:
    symbols = await market_breadth_symbols(datahub)
    if not symbols:
        return []
    quotes: list[Quote] = []
    for index in range(0, len(symbols), MARKET_BREADTH_BATCH_SIZE):
        batch = symbols[index : index + MARKET_BREADTH_BATCH_SIZE]
        try:
            quotes.extend(await datahub.quotes(batch))
            continue
        except Exception:
            pass
        for symbol in batch:
            try:
                quotes.extend(await datahub.quotes([symbol]))
            except Exception:
                continue
    return dedupe_quotes(quotes)


async def market_breadth_symbols(datahub) -> list[str]:
    try:
        pool = await datahub.stock_pool(limit=1200, refresh=False)
    except Exception:
        pool = []
    seed_symbols = list(datahub.settings.seed_symbols)
    pool_symbols = stratified_market_breadth_symbols(
        pool,
        max(0, MARKET_BREADTH_LIMIT - len(seed_symbols)),
        seed_symbols,
    )
    return list(dict.fromkeys([*seed_symbols, *pool_symbols]))[:MARKET_BREADTH_LIMIT]


def stratified_market_breadth_symbols(
    pool: list[StockInfo],
    limit: int,
    seed_symbols: list[str] | None = None,
) -> list[str]:
    if limit <= 0:
        return []
    seed_symbols = seed_symbols or []
    groups = {
        "SH": sorted({item.symbol for item in pool if item.market == "SH" and item.symbol}),
        "SZ": sorted({item.symbol for item in pool if item.market == "SZ" and item.symbol}),
    }
    seed_codes = {item.split(".")[0] for item in seed_symbols if "." in item}
    industry_groups = industry_symbol_groups(pool, exclude_codes=seed_codes)
    market_quota = max(1, round(limit * 0.55 / 2))
    industry_quota = max(1, round(limit * 0.45 / max(1, len(industry_groups))))
    picked = [*even_sample(groups["SH"], market_quota), *even_sample(groups["SZ"], market_quota)]
    for industry_symbols in industry_groups.values():
        picked.extend(even_sample(industry_symbols, industry_quota))
    if len(picked) < limit:
        remaining = sorted({symbol for symbols in groups.values() for symbol in symbols} - set(picked))
        picked.extend(even_sample(remaining, limit - len(picked)))
    return list(dict.fromkeys(picked))[:limit]


def industry_symbol_groups(pool: list[StockInfo], exclude_codes: set[str] | None = None) -> dict[str, list[str]]:
    exclude_codes = exclude_codes or set()
    grouped: dict[str, list[str]] = {}
    for item in pool:
        if not item.industry or not item.symbol or item.code in exclude_codes:
            continue
        grouped.setdefault(item.industry, []).append(item.symbol)
    return {name: sorted(set(symbols)) for name, symbols in sorted(grouped.items())[:10]}


def even_sample(items: list[str], limit: int) -> list[str]:
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return items[:]
    if limit == 1:
        return [items[len(items) // 2]]
    step = (len(items) - 1) / (limit - 1)
    picked: list[str] = []
    for index in range(limit):
        item = items[round(index * step)]
        if item not in picked:
            picked.append(item)
    cursor = 0
    while len(picked) < limit and cursor < len(items):
        item = items[cursor]
        if item not in picked:
            picked.append(item)
        cursor += 1
    return picked[:limit]


def dedupe_quotes(quotes: Iterable[Quote]) -> list[Quote]:
    by_symbol: dict[str, Quote] = {}
    for quote in quotes:
        code = getattr(quote, "code", "")
        market = getattr(quote, "market", "")
        if code and market:
            by_symbol[f"{code}.{market}"] = quote
    return list(by_symbol.values())


async def peer_quotes(datahub, profile: StockInfo | None, target_symbol: str) -> list[Quote]:
    if not profile or not profile.industry:
        return []
    try:
        pool = await datahub.stock_pool(limit=1200, refresh=False)
    except Exception:
        return []
    peers = [
        item.symbol
        for item in pool
        if item.industry == profile.industry and item.symbol != target_symbol and item.market in {"SH", "SZ"}
    ]
    selected = even_sample(sorted(set(peers)), PEER_QUOTE_LIMIT)
    if not selected:
        return []
    quotes: list[Quote] = []
    for index in range(0, len(selected), MARKET_BREADTH_BATCH_SIZE):
        batch = selected[index : index + MARKET_BREADTH_BATCH_SIZE]
        try:
            quotes.extend(await datahub.quotes(batch))
        except Exception:
            for symbol in batch:
                try:
                    quotes.extend(await datahub.quotes([symbol]))
                except Exception:
                    continue
    return dedupe_quotes(quotes)


def unique_standard_symbols(symbols: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        try:
            normalized = standard_symbol(symbol)
        except ValueError:
            continue
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result
