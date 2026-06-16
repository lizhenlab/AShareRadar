from __future__ import annotations

import importlib.util
from typing import Any

from app.utils.symbols import normalize_symbol


def is_installed(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def pick(row: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            value = row[name]
        except Exception:
            continue
        if value is not None and value == value:
            return value
    return default


def ak_symbol(symbol: str) -> str:
    code, _ = normalize_symbol(symbol)
    return code


def ts_symbol(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    return f"{code}.{market.upper()}"


def bs_symbol(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    return f"{market}.{code}"
