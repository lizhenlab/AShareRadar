from __future__ import annotations

from collections.abc import Iterable
import math
from typing import TypeVar


T = TypeVar("T")


QUOTE_REQUIRED_FINITE_FIELDS = (
    "change",
    "change_pct",
)
QUOTE_OPTIONAL_FINITE_FIELDS = ("pe", "pb", "market_cap")


def finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def valid_positive_number(value: object) -> bool:
    parsed = finite_float(value)
    return parsed is not None and parsed > 0


def valid_non_negative_number(value: object) -> bool:
    parsed = finite_float(value)
    return parsed is not None and parsed >= 0


def valid_optional_non_negative_number(value: object) -> bool:
    return value is None or valid_non_negative_number(value)


def valid_ohlc(open_price: object, close: object, high: object, low: object) -> bool:
    open_value = finite_float(open_price)
    close_value = finite_float(close)
    high_value = finite_float(high)
    low_value = finite_float(low)
    if None in (open_value, close_value, high_value, low_value):
        return False
    assert open_value is not None and close_value is not None and high_value is not None and low_value is not None
    return (
        open_value > 0
        and close_value > 0
        and high_value > 0
        and low_value > 0
        and high_value >= max(open_value, close_value)
        and low_value <= min(open_value, close_value)
    )


def valid_kline(row: object) -> bool:
    return _valid_bar(row) and valid_non_negative_number(getattr(row, "volume", None))


def valid_minute_kline(row: object) -> bool:
    return (
        valid_kline(row)
        and valid_optional_non_negative_number(getattr(row, "amount", None))
        and valid_optional_non_negative_number(getattr(row, "turnover_rate", None))
    )


def valid_quote(quote: object) -> bool:
    return (
        valid_ohlc(
            getattr(quote, "open", None),
            getattr(quote, "price", None),
            getattr(quote, "high", None),
            getattr(quote, "low", None),
        )
        and valid_positive_number(getattr(quote, "prev_close", None))
        and valid_non_negative_number(getattr(quote, "volume", None))
        and valid_non_negative_number(getattr(quote, "amount", None))
        and all(_finite_attr(quote, field) for field in QUOTE_REQUIRED_FINITE_FIELDS)
        and valid_optional_non_negative_number(getattr(quote, "turnover_rate", None))
        and all(_optional_finite_attr(quote, field) for field in QUOTE_OPTIONAL_FINITE_FIELDS)
    )


def filter_valid_klines(rows: Iterable[T]) -> list[T]:
    return [row for row in rows if valid_kline(row)]


def filter_valid_minute_klines(rows: Iterable[T]) -> list[T]:
    return [row for row in rows if valid_minute_kline(row)]


def filter_valid_quotes(rows: Iterable[T]) -> list[T]:
    return [row for row in rows if valid_quote(row)]


def _valid_bar(row: object) -> bool:
    return valid_ohlc(
        getattr(row, "open", None),
        getattr(row, "close", None),
        getattr(row, "high", None),
        getattr(row, "low", None),
    )


def _finite_attr(row: object, field: str) -> bool:
    return finite_float(getattr(row, field, None)) is not None


def _optional_finite_attr(row: object, field: str) -> bool:
    value = getattr(row, field, None)
    return value is None or finite_float(value) is not None


__all__ = [
    "QUOTE_OPTIONAL_FINITE_FIELDS",
    "QUOTE_REQUIRED_FINITE_FIELDS",
    "filter_valid_klines",
    "filter_valid_minute_klines",
    "filter_valid_quotes",
    "finite_float",
    "valid_kline",
    "valid_minute_kline",
    "valid_non_negative_number",
    "valid_ohlc",
    "valid_optional_non_negative_number",
    "valid_positive_number",
    "valid_quote",
]
