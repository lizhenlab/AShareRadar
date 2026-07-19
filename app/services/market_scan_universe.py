from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.models.schemas import StockInfo
from app.models.market_scan import MarketScanSeed
from app.utils.symbols import is_a_share_stock_code, standard_symbol


FULL_MARKET_SCOPE = "沪市 + 深市 + 北交所当前上市A股"
FULL_MARKET_MARKETS = frozenset({"SH", "SZ", "BJ"})


@dataclass(frozen=True)
class MarketScanUniverse:
    seeds: tuple[MarketScanSeed, ...]
    excluded_count: int


def build_market_scan_universe(
    rows: list[StockInfo],
    *,
    data_date: date,
    new_stock_days: int,
) -> MarketScanUniverse:
    by_symbol: dict[str, MarketScanSeed] = {}
    excluded_count = 0
    for row in rows:
        seed = _market_scan_seed(row, data_date=data_date, new_stock_days=new_stock_days)
        if seed is None:
            excluded_count += 1
            continue
        if seed.symbol in by_symbol:
            excluded_count += 1
            continue
        by_symbol[seed.symbol] = seed
    seeds = tuple(sorted(by_symbol.values(), key=lambda item: (item.market, item.code, item.symbol)))
    return MarketScanUniverse(seeds=seeds, excluded_count=excluded_count)


def _market_scan_seed(
    row: StockInfo,
    *,
    data_date: date,
    new_stock_days: int,
) -> MarketScanSeed | None:
    code = str(row.code or "").strip()
    market = str(row.market or "").strip().upper()
    name = " ".join(str(row.name or "").split()).strip()
    if market not in FULL_MARKET_MARKETS or not is_a_share_stock_code(code, market) or not name or _is_delisted_name(name):
        return None
    try:
        symbol = standard_symbol(f"{code}.{market}")
        if standard_symbol(row.symbol) != symbol or standard_symbol(code) != symbol:
            return None
    except ValueError:
        return None
    code, canonical_market = symbol.split(".", 1)
    if canonical_market != market:
        return None
    list_date = _parse_list_date(row.list_date)
    is_new = bool(list_date and 0 <= (data_date - list_date).days <= new_stock_days)
    return MarketScanSeed(
        symbol=symbol,
        code=code,
        market=market,
        name=name,
        industry=_clean_optional_text(row.industry),
        list_date=list_date.isoformat() if list_date is not None else None,
        is_st="ST" in name.upper(),
        is_new=is_new,
        metadata_source=_clean_optional_text(row.source),
    )


def _parse_list_date(value: object) -> date | None:
    text = str(value or "").strip()
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _is_delisted_name(name: str) -> bool:
    normalized = name.replace(" ", "")
    return "退市" in normalized or normalized.endswith("退")


def _clean_optional_text(value: object) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text or None


__all__ = [
    "FULL_MARKET_MARKETS",
    "FULL_MARKET_SCOPE",
    "MarketScanUniverse",
    "build_market_scan_universe",
]
