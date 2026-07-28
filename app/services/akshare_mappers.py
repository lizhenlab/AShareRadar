from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.models.market import (
    MinuteKline,
    Quote,
    StockInfo,
)
from app.services.provider_utils import pick, valid_ohlc
from app.services.provider_stock_mappers import stock_code_from_value
from app.utils.parsing import safe_float
from app.utils.stock_pool import normalize_stock_industry_text, normalize_stock_metadata_text
from app.utils.symbols import normalize_symbol, standard_symbol


AKSHARE_STOCK_INDUSTRY_FIELDS = (
    "industry",
    "industry_name",
    "industryName",
    "所属行业",
    "所属行业名称",
    "行业",
    "行业名称",
    "行业分类",
    "证监会行业",
    "证监会行业名称",
    "证监会行业分类",
    "申万行业",
    "申万一级行业",
    "申万一级行业名称",
)
AKSHARE_STOCK_LIST_DATE_FIELDS = (
    "list_date",
    "上市日期",
    "A股上市日期",
    "上市时间",
    "挂牌日期",
    "首发上市日期",
)


@dataclass(frozen=True)
class AkshareQuoteFields:
    code: str
    name: str
    market: str
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    volume: float
    amount: float
    change: float
    change_pct: float
    turnover_rate: float | None
    pe: float | None
    pb: float | None
    market_cap: float | None


def akshare_code(value: Any) -> str | None:
    return stock_code_from_value(value)


def quote_from_spot_row(row: Any, *, stamp: str, source_name: str) -> Quote | None:
    fields = _akshare_quote_fields(row)
    if fields is None:
        return None
    return _quote_from_fields(fields, stamp=stamp, source_name=source_name)


def _akshare_quote_fields(row: Any) -> AkshareQuoteFields | None:
    code = akshare_code(pick(row, "代码", "code", "symbol", default=""))
    if not code:
        return None
    price = _row_positive_float(row, "最新价", "price")
    if price is None:
        return None
    prev_close = _price_or_fallback(_row_positive_float(row, "昨收", "prev_close"), price)
    change = _row_float(row, "涨跌额", "change", default=price - prev_close)
    market = normalize_symbol(code)[1].upper()
    return AkshareQuoteFields(
        code=code,
        name=str(pick(row, "名称", "name", default=code)),
        market=market,
        price=price,
        prev_close=prev_close,
        open=_price_or_fallback(_row_positive_float(row, "今开", "open"), price),
        high=_price_or_fallback(_row_positive_float(row, "最高", "high"), price),
        low=_price_or_fallback(_row_positive_float(row, "最低", "low"), price),
        volume=_row_float(row, "成交量", "volume"),
        amount=_row_float(row, "成交额", "amount"),
        change=change,
        change_pct=_quote_change_pct(row, price, prev_close),
        turnover_rate=_optional_row_float(row, "换手率", "turnover_rate"),
        pe=_optional_row_float(row, "市盈率-动态", "市盈率", "pe"),
        pb=_optional_row_float(row, "市净率", "pb"),
        market_cap=_optional_row_float(row, "总市值", "market_cap"),
    )


def _quote_from_fields(fields: AkshareQuoteFields, *, stamp: str, source_name: str) -> Quote:
    return Quote(
        code=fields.code,
        name=fields.name,
        market=fields.market,
        price=fields.price,
        prev_close=fields.prev_close,
        open=fields.open,
        high=fields.high,
        low=fields.low,
        volume=fields.volume,
        amount=fields.amount,
        change=fields.change,
        change_pct=fields.change_pct,
        turnover_rate=fields.turnover_rate,
        pe=fields.pe,
        pb=fields.pb,
        market_cap=fields.market_cap,
        timestamp=stamp,
        source=source_name,
    )


def _row_float(row: Any, *names: str, default: float = 0) -> float:
    return safe_float(str(pick(row, *names, default=default)))


def _row_positive_float(row: Any, *names: str) -> float | None:
    value = _row_float(row, *names)
    return value if value > 0 else None


def _price_or_fallback(value: float | None, fallback: float) -> float:
    return value if value is not None else fallback


def _optional_row_float(row: Any, *names: str) -> float | None:
    value = _row_float(row, *names)
    return value or None


def _quote_change_pct(row: Any, price: float, prev_close: float) -> float:
    explicit = pick(row, "涨跌幅", "change_pct", default=None)
    if explicit is not None:
        return safe_float(str(explicit))
    return (price - prev_close) / prev_close * 100 if prev_close > 0 else 0


def minute_kline_from_hist_row(row: Any, *, interval: str, source_name: str) -> MinuteKline | None:
    timestamp = str(pick(row, "时间", default="")).strip()
    open_price = safe_float(str(pick(row, "开盘", default=0)))
    close = safe_float(str(pick(row, "收盘", default=0)))
    high = safe_float(str(pick(row, "最高", default=0)))
    low = safe_float(str(pick(row, "最低", default=0)))
    if not timestamp or not valid_ohlc(open_price, close, high, low):
        return None
    return MinuteKline(
        timestamp=timestamp,
        open=open_price,
        close=close,
        high=high,
        low=low,
        volume=safe_float(str(pick(row, "成交量", default=0))),
        amount=safe_float(str(pick(row, "成交额", default=0))) or None,
        turnover_rate=safe_float(str(pick(row, "换手率", default=0))) or None,
        interval=interval,
        source=source_name,
    )


def minute_klines_from_hist_rows(rows: Any, *, interval: str, source_name: str) -> list[MinuteKline]:
    result: list[MinuteKline] = []
    for row in rows:
        item = minute_kline_from_hist_row(row, interval=interval, source_name=source_name)
        if item is not None:
            result.append(item)
    return result


def stock_info_from_code_name_row(row: Any, *, stamp: str, source_name: str) -> StockInfo | None:
    code = akshare_code(
        pick(row, "code", "代码", "symbol", "证券代码", "A股代码", default="")
    )
    if not code:
        return None
    market = normalize_symbol(code)[1].upper()
    return StockInfo(
        symbol=standard_symbol(code),
        code=code,
        market=market,
        name=str(pick(row, "name", "名称", "证券简称", "A股简称", default=code)),
        industry=_optional_stock_text(_stock_row_value(row, AKSHARE_STOCK_INDUSTRY_FIELDS)),
        list_date=_stock_list_date_from_row(row),
        source=source_name,
        updated_at=stamp,
    )


def _optional_stock_text(value: Any) -> str | None:
    return normalize_stock_industry_text(value)


def _stock_row_value(row: Any, fields: tuple[str, ...]) -> Any:
    return next(iter(_stock_row_values(row, fields)), None)


def _stock_row_values(row: Any, fields: tuple[str, ...]) -> list[Any]:
    values: list[Any] = []
    for field in fields:
        value = _row_value(row, field)
        if normalize_stock_metadata_text(value) is not None:
            values.append(value)
    normalized_fields = {_normalize_stock_field_name(field) for field in fields}
    for key in _row_keys(row):
        if str(key) in fields:
            continue
        if _normalize_stock_field_name(key) not in normalized_fields:
            continue
        value = _row_value(row, key)
        if normalize_stock_metadata_text(value) is not None:
            values.append(value)
    return values


def _row_value(row: Any, field: Any) -> Any:
    try:
        return row[field]
    except (KeyError, IndexError, TypeError):
        return None


def _row_keys(row: Any) -> tuple[Any, ...]:
    keys = getattr(row, "keys", None)
    if not callable(keys):
        return ()
    try:
        return tuple(keys())
    except (AttributeError, TypeError):
        return ()


def _normalize_stock_field_name(value: Any) -> str:
    text = normalize_stock_metadata_text(value) or ""
    return text.replace("_", "").replace("-", "").casefold()


def _stock_list_date(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        text = str(isoformat()).strip()[:10]
    else:
        text = normalize_stock_metadata_text(value) or ""
    if not text:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if len(text) >= 10 and text[4:5] in {"-", "/"} and text[7:8] in {"-", "/"}:
        text = text[:10]
    compact = text.replace("-", "").replace("/", "")
    try:
        return datetime.strptime(compact, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def _stock_list_date_from_row(row: Any) -> str | None:
    for value in _stock_row_values(row, AKSHARE_STOCK_LIST_DATE_FIELDS):
        list_date = _stock_list_date(value)
        if list_date is not None:
            return list_date
    return None


__all__ = [
    "AKSHARE_STOCK_INDUSTRY_FIELDS",
    "AKSHARE_STOCK_LIST_DATE_FIELDS",
    "akshare_code",
    "minute_kline_from_hist_row",
    "minute_klines_from_hist_rows",
    "quote_from_spot_row",
    "stock_info_from_code_name_row",
]
