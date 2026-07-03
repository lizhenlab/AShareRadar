from __future__ import annotations

from typing import Any

from app.models.schemas import MinuteKline, Quote
from app.services.provider_stock_mappers import stock_code_from_value
from app.services.provider_utils import pick, valid_ohlc
from app.utils.parsing import MISSING_NUMERIC_VALUES, required_float, safe_float
from app.utils.symbols import normalize_symbol


_SUPPORTED_MARKETS = {"SH", "SZ"}
_MISSING = object()


def futu_symbol(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    return f"{market.upper()}.{code}"


def quote_from_snapshot_row(row: Any, *, stamp: str, source_name: str) -> Quote | None:
    parsed = _parse_futu_code(pick(row, "code", default=""))
    if parsed is None:
        return None
    market, code = parsed
    try:
        price = _required_row_float(row, "last_price", field="Futu现价")
        prev_close = _required_row_float(row, "prev_close_price", field="Futu昨收")
        open_price = _row_float_or_default(row, "open_price", default=price, field="Futu开盘价")
        high = _row_float_or_default(row, "high_price", default=price, field="Futu最高价")
        low = _row_float_or_default(row, "low_price", default=price, field="Futu最低价")
        _ensure_quote_price_bounds(price, high, low)
    except ValueError:
        return None
    return Quote(
        code=code,
        name=str(pick(row, "stock_name", default=code)),
        market=market,
        price=price,
        prev_close=prev_close,
        open=open_price,
        high=high,
        low=low,
        volume=safe_float(str(pick(row, "volume", default=0))),
        amount=safe_float(str(pick(row, "turnover", default=0))),
        change=round(price - prev_close, 4),
        change_pct=safe_float(str(pick(row, "change_rate", default=0))),
        turnover_rate=safe_float(str(pick(row, "turnover_rate", default=0))) or None,
        pe=safe_float(str(pick(row, "pe_ratio", default=0))) or None,
        pb=None,
        market_cap=None,
        timestamp=stamp,
        source=source_name,
    )


def minute_kline_from_row(row: Any, *, interval: str, source_name: str) -> MinuteKline | None:
    open_price = safe_float(str(pick(row, "open", default=0)))
    close = safe_float(str(pick(row, "close", default=0)))
    high = safe_float(str(pick(row, "high", default=0)))
    low = safe_float(str(pick(row, "low", default=0)))
    item = MinuteKline(
        timestamp=str(pick(row, "time_key", default="")),
        open=open_price,
        close=close,
        high=high,
        low=low,
        volume=safe_float(str(pick(row, "volume", default=0))),
        amount=safe_float(str(pick(row, "turnover", default=0))) or None,
        turnover_rate=safe_float(str(pick(row, "turnover_rate", default=0))) or None,
        interval=interval,
        source=source_name,
    )
    if not item.timestamp or not valid_ohlc(item.open, item.close, item.high, item.low):
        return None
    return item


def _parse_futu_code(value: Any) -> tuple[str, str] | None:
    raw = str(value or "").strip().upper()
    if "." not in raw:
        return None
    market, raw_code = raw.split(".", 1)
    if market not in _SUPPORTED_MARKETS:
        return None
    code = stock_code_from_value(raw_code)
    if not code:
        return None
    return market, code


def _required_row_float(row: Any, *names: str, field: str) -> float:
    return required_float(pick(row, *names, default=_MISSING), field, positive=True)


def _row_float_or_default(row: Any, *names: str, default: float, field: str) -> float:
    value = pick(row, *names, default=_MISSING)
    if value is _MISSING or value in MISSING_NUMERIC_VALUES:
        return default
    return required_float(value, field, positive=True)


def _ensure_quote_price_bounds(price: float, high: float, low: float) -> None:
    if high < low:
        raise ValueError("Futu最高价低于最低价")
    if price > high or price < low:
        raise ValueError("Futu现价超出最高/最低价范围")


__all__ = ["futu_symbol", "minute_kline_from_row", "quote_from_snapshot_row"]
