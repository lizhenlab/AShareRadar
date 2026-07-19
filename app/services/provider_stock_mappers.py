from __future__ import annotations

import re
from typing import Any

from app.models.schemas import StockInfo
from app.services.provider_utils import pick
from app.utils.symbols import normalize_symbol, standard_symbol


_CODE_PATTERN = re.compile(r"(\d{6})")
_SUPPORTED_MARKETS = {"SH", "SZ", "BJ"}


def stock_code_from_value(value: Any) -> str | None:
    text = str(value or "").strip()
    match = _CODE_PATTERN.search(text)
    if match:
        code = match.group(1)
    else:
        numeric = re.fullmatch(r"(\d{1,6})(?:\.0+)?", text)
        if numeric is None:
            return None
        code = numeric.group(1).zfill(6)
    if code == "000000":
        return None
    try:
        normalize_symbol(code)
    except ValueError:
        return None
    return code


def stock_info_from_baostock_row(row: dict[str, Any], *, stamp: str, source_name: str) -> StockInfo | None:
    stock_type = str(row.get("type") or "").strip()
    listing_status = str(row.get("status") or "").strip()
    out_date = str(row.get("outDate") or "").strip()
    if stock_type not in {"", "1"} or listing_status not in {"", "1"} or out_date:
        return None
    market_code = _baostock_market_code(row.get("code", ""))
    if market_code is None:
        return None
    market, code = market_code
    list_date = _compact_date(row.get("ipoDate", ""))
    name = str(row.get("code_name") or code)
    return _stock_info(code, market, name, None, list_date, stamp, source_name)


def _baostock_market_code(value: Any) -> tuple[str, str] | None:
    raw_code = str(value or "").strip()
    if "." not in raw_code:
        return None
    market, raw_symbol = raw_code.split(".", 1)
    market = market.upper()
    code = stock_code_from_value(raw_symbol)
    if not code or market not in _SUPPORTED_MARKETS:
        return None
    return market, code


def _stock_info(
    code: str,
    market: str,
    name: str,
    industry: str | None,
    list_date: str | None,
    stamp: str,
    source_name: str,
) -> StockInfo:
    return StockInfo(
        symbol=standard_symbol(f"{code}.{market.upper()}"),
        code=code,
        market=market.upper(),
        name=name,
        industry=industry,
        list_date=list_date,
        source=source_name,
        updated_at=stamp,
    )


def stock_info_from_tushare_row(row: Any, *, stamp: str, source_name: str) -> StockInfo | None:
    raw_symbol = str(pick(row, "ts_code", default="")).strip()
    code = stock_code_from_value(raw_symbol or pick(row, "symbol", default=""))
    if not code:
        return None
    market = _tushare_market(raw_symbol, code)
    if market is None:
        return None
    name = str(pick(row, "name", default=code))
    industry = str(pick(row, "industry", default="")) or None
    list_date = _compact_date(pick(row, "list_date", default=""))
    return _stock_info(code, market, name, industry, list_date, stamp, source_name)


def _tushare_market(raw_symbol: str, code: str) -> str | None:
    try:
        _, market = normalize_symbol(raw_symbol or code)
    except ValueError:
        return None
    return market.upper()


def _compact_date(value: Any) -> str | None:
    raw = str(value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw or None


__all__ = [
    "stock_code_from_value",
    "stock_info_from_baostock_row",
    "stock_info_from_tushare_row",
]
