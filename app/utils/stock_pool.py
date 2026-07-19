from __future__ import annotations

from collections.abc import Iterable

from app.models.schemas import StockInfo
from app.utils.symbols import is_a_share_stock_code, standard_symbol


def normalize_stock_pool_rows(rows: Iterable[StockInfo]) -> list[StockInfo]:
    normalized: list[StockInfo] = []
    seen_symbols: set[str] = set()
    for item in rows:
        row = normalize_stock_pool_row(item)
        if row is None or row.symbol in seen_symbols:
            continue
        seen_symbols.add(row.symbol)
        normalized.append(row)
    return normalized


def normalize_stock_pool_row(item: StockInfo) -> StockInfo | None:
    raw_symbol = _required_text(item.symbol)
    code = _required_text(item.code)
    market = _required_text(item.market).upper()
    name = _required_text(item.name)
    source = _required_text(item.source)
    updated_at = _required_text(item.updated_at)
    if not all((raw_symbol, code, market, name, source, updated_at)):
        return None
    try:
        symbol = standard_symbol(raw_symbol)
    except (AttributeError, TypeError, ValueError):
        return None
    if symbol != f"{code}.{market}" or not is_a_share_stock_code(code, market):
        return None
    return item.model_copy(
        update={
            "symbol": symbol,
            "code": code,
            "market": market,
            "name": name,
            "industry": _optional_text(item.industry),
            "list_date": _optional_text(item.list_date),
            "source": source,
            "updated_at": updated_at,
        }
    )


def _required_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _optional_text(value: object) -> str | None:
    return _required_text(value) or None


__all__ = ["normalize_stock_pool_row", "normalize_stock_pool_rows"]
