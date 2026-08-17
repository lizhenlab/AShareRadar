from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re

from app.models.market import (
    StockInfo,
)
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE, MarketScanSeed
from app.utils.stock_pool import normalize_stock_metadata_text
from app.utils.symbols import is_a_share_stock_code, standard_symbol


FULL_MARKET_SCOPE = MARKET_SCAN_FULL_MARKET_SCOPE
FULL_MARKET_MARKETS = frozenset({"SH", "SZ", "BJ"})
_ST_NAME_PREFIX = re.compile(r"^(?:S\*ST|\*ST|SST|ST)(?=[\u3400-\u9fff])", re.IGNORECASE)


@dataclass(frozen=True)
class MarketScanUniverse:
    seeds: tuple[MarketScanSeed, ...]
    excluded_count: int
    unknown_list_date_count: int = 0
    future_list_date_count: int = 0


@dataclass(frozen=True)
class _MarketScanCandidate:
    seed: MarketScanSeed | None
    list_date_state: str = "excluded"


@dataclass(frozen=True)
class _MarketScanIdentity:
    symbol: str
    code: str
    market: str
    name: str


def build_market_scan_universe(
    rows: list[StockInfo],
    *,
    data_date: date,
    new_stock_days: int,
) -> MarketScanUniverse:
    if isinstance(new_stock_days, bool) or new_stock_days < 0:
        raise ValueError("新股天数必须为非负整数")
    by_symbol: dict[str, MarketScanSeed] = {}
    excluded_count = 0
    unknown_list_date_count = 0
    future_list_date_count = 0
    for row in rows:
        candidate = _market_scan_candidate(row, data_date=data_date, new_stock_days=new_stock_days)
        seed = candidate.seed
        if seed is None:
            excluded_count += 1
            future_list_date_count += candidate.list_date_state == "future"
            continue
        if seed.symbol in by_symbol:
            if by_symbol[seed.symbol] != seed:
                raise ValueError(
                    f"股票池同一股票存在冲突元数据：{seed.symbol}"
                )
            excluded_count += 1
            continue
        by_symbol[seed.symbol] = seed
        unknown_list_date_count += candidate.list_date_state == "unknown"
    seeds = tuple(sorted(by_symbol.values(), key=lambda item: (item.market, item.code, item.symbol)))
    return MarketScanUniverse(
        seeds=seeds,
        excluded_count=excluded_count,
        unknown_list_date_count=unknown_list_date_count,
        future_list_date_count=future_list_date_count,
    )


def _market_scan_candidate(
    row: StockInfo,
    *,
    data_date: date,
    new_stock_days: int,
) -> _MarketScanCandidate:
    identity = _market_scan_identity(row)
    if identity is None:
        return _MarketScanCandidate(None)
    list_date = _parse_list_date(row.list_date)
    if list_date is not None and list_date > data_date:
        return _MarketScanCandidate(None, "future")
    list_date_state = "known" if list_date is not None else "unknown"
    return _MarketScanCandidate(
        _candidate_seed(
            row,
            identity=identity,
            list_date=list_date,
            data_date=data_date,
            new_stock_days=new_stock_days,
        ),
        list_date_state,
    )


def _market_scan_identity(row: StockInfo) -> _MarketScanIdentity | None:
    code = str(row.code or "").strip()
    market = str(row.market or "").strip().upper()
    name = " ".join(str(row.name or "").split()).strip()
    if not _is_supported_stock_identity(code=code, market=market, name=name):
        return None
    try:
        symbol = standard_symbol(f"{code}.{market}")
        if not _matches_canonical_symbol(row, code=code, symbol=symbol):
            return None
    except ValueError:
        return None
    code, canonical_market = symbol.split(".", 1)
    if canonical_market != market:
        return None
    return _MarketScanIdentity(
        symbol=symbol,
        code=code,
        market=market,
        name=name,
    )


def _is_supported_stock_identity(*, code: str, market: str, name: str) -> bool:
    return (
        market in FULL_MARKET_MARKETS
        and is_a_share_stock_code(code, market)
        and bool(name)
        and not _is_delisted_name(name)
    )


def _matches_canonical_symbol(row: StockInfo, *, code: str, symbol: str) -> bool:
    return standard_symbol(row.symbol) == symbol and standard_symbol(code) == symbol


def _candidate_seed(
    row: StockInfo,
    *,
    identity: _MarketScanIdentity,
    list_date: date | None,
    data_date: date,
    new_stock_days: int,
) -> MarketScanSeed:
    age_days = (data_date - list_date).days if list_date is not None else None
    return MarketScanSeed(
        symbol=identity.symbol,
        code=identity.code,
        market=identity.market,
        name=identity.name,
        industry=_clean_optional_text(row.industry),
        list_date=list_date.isoformat() if list_date is not None else None,
        is_st=_is_st_name(identity.name),
        is_new=age_days is not None and age_days <= new_stock_days,
        metadata_source=_clean_optional_text(row.source),
    )


def _parse_list_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = normalize_stock_metadata_text(value)
    if text is None:
        return None
    compact = text.replace("-", "").replace("/", "")
    try:
        return datetime.strptime(compact, "%Y%m%d").date()
    except ValueError:
        return None


def _is_st_name(name: str) -> bool:
    normalized = normalize_stock_metadata_text(name) or ""
    return bool(_ST_NAME_PREFIX.match(normalized))


def _is_delisted_name(name: str) -> bool:
    normalized = name.replace(" ", "")
    return "退市" in normalized or normalized.endswith("退")


def _clean_optional_text(value: object) -> str | None:
    return normalize_stock_metadata_text(value)


__all__ = [
    "FULL_MARKET_MARKETS",
    "FULL_MARKET_SCOPE",
    "MarketScanUniverse",
    "build_market_scan_universe",
]
