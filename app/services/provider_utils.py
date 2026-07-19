from __future__ import annotations

import importlib.util
from typing import Any

from app.services.provider_errors import ProviderCoverageMiss
from app.utils.market_data import valid_ohlc as _valid_ohlc
from app.utils.symbols import normalize_symbol


ROW_FIELD_MISSING_ERRORS = (KeyError, IndexError, TypeError)


def is_installed(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def pick(row: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            value = row[name]
        except ROW_FIELD_MISSING_ERRORS:
            continue
        if value is not None and value == value:
            return value
    return default


def ensure_positive_limit(limit: int, label: str = "limit") -> None:
    if limit <= 0:
        raise ValueError(f"{label} 必须大于 0")


def valid_ohlc(open_price: object, close: object, high: object, low: object) -> bool:
    return _valid_ohlc(open_price, close, high, low)


def ak_symbol(symbol: str) -> str:
    code, _ = normalize_symbol(symbol)
    return code


def ts_symbol(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    return f"{code}.{market.upper()}"


def bs_symbol(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    if market == "bj":
        raise ProviderCoverageMiss(f"BaoStock 当前不覆盖北交所股票：{code}.BJ")
    return f"{market}.{code}"
