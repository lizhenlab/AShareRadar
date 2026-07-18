from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
import threading
from typing import Any
from urllib.parse import urlsplit

import requests

from app.models.schemas import Kline, MinuteKline, Quote
from app.services.data_quality_time import normalize_quote_event_time
from app.services.provider_errors import (
    ProviderCoverageMiss,
    ProviderError,
    ProviderProtocolError,
    ProviderTransportError,
    sanitize_provider_error,
)
from app.services.provider_utils import ensure_positive_limit
from app.utils.market_data import valid_kline, valid_minute_kline
from app.utils.parsing import MISSING_NUMERIC_VALUES, required_float, safe_float
from app.utils.symbols import normalize_symbol, standard_symbol


EASTMONEY_BRIDGE_SOURCE_NAME = "AKShare·东方财富直连"
EASTMONEY_NO_PROXY_HOSTS = (
    "eastmoney.com",
    ".eastmoney.com",
    "*.eastmoney.com",
    "82.push2.eastmoney.com",
    "23.push2.eastmoney.com",
    "53.push2.eastmoney.com",
    "push2his.eastmoney.com",
)
EASTMONEY_QUOTE_HOSTS = ("82.push2.eastmoney.com", "23.push2.eastmoney.com", "53.push2.eastmoney.com")
EASTMONEY_SCHEMES = ("https",)
EASTMONEY_HIST_HOST = "push2his.eastmoney.com"
EASTMONEY_UT_PARAM = "bd1d9ddb04089700cf9c27f6f7426281"
EASTMONEY_HISTORY_UT_PARAM = "7eea3edcaed734bea9cbfc24409ed989"
EASTMONEY_QUOTE_FIELDS = (
    "f1",
    "f2",
    "f3",
    "f4",
    "f5",
    "f6",
    "f7",
    "f8",
    "f9",
    "f10",
    "f12",
    "f13",
    "f14",
    "f15",
    "f16",
    "f17",
    "f18",
    "f20",
    "f21",
    "f23",
    "f86",
    "f115",
    "f152",
)
EASTMONEY_HISTORY_FIELDS1 = "f1,f2,f3,f4,f5,f6"
EASTMONEY_HISTORY_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
EASTMONEY_DAILY_PERIOD = "101"
EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json,text/plain,*/*",
    "Connection": "close",
}
_EASTMONEY_ENV_LOCK = threading.RLock()


@dataclass(frozen=True)
class EastmoneyQuoteFields:
    code: str
    market: str
    name: str
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
    timestamp: str


@contextmanager
def eastmoney_no_proxy():
    with _EASTMONEY_ENV_LOCK:
        previous = {key: os.environ.get(key) for key in ("NO_PROXY", "no_proxy")}
        merged = merge_no_proxy(previous["NO_PROXY"] or previous["no_proxy"])
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def merge_no_proxy(existing: str | None) -> str:
    values = [item.strip() for item in str(existing or "").split(",") if item.strip()]
    seen = {item.lower() for item in values}
    for host in EASTMONEY_NO_PROXY_HOSTS:
        if host.lower() not in seen:
            values.append(host)
            seen.add(host.lower())
    return ",".join(values)


def eastmoney_get_json(url: str, params: dict[str, Any], timeout: float = 8) -> dict[str, Any]:
    _require_https_url(url)
    try:
        with _eastmoney_session() as session:
            response = session.get(url, params=params, headers=EASTMONEY_HEADERS, timeout=timeout)
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                raise ProviderProtocolError("东方财富接口返回非 JSON 响应") from None
    except ProviderError:
        raise
    except requests.RequestException as exc:
        raise ProviderTransportError(sanitize_provider_error(exc)) from exc
    except Exception as exc:
        raise ProviderTransportError(sanitize_provider_error(exc)) from exc
    if not isinstance(data, dict):
        raise ProviderProtocolError("东方财富接口返回结构异常")
    if data.get("rc") != 0:
        detail = sanitize_provider_error(f"rc={data.get('rc')} {data.get('rt')}")
        raise ProviderProtocolError(f"东方财富接口返回异常：{detail}")
    return data


def eastmoney_quotes(symbols) -> list[Quote]:
    requested = eastmoney_requested_symbols(symbols)
    if not requested:
        return []
    params = eastmoney_quote_params(requested)
    errors: list[str] = []
    endpoint_responded = False
    covered_quotes: list[Quote] = []
    for url in eastmoney_quote_urls():
        error_count = len(errors)
        quotes = eastmoney_quotes_from_url(url, params, errors)
        covered_quotes.extend(quotes)
        endpoint_responded = endpoint_responded or bool(quotes) or len(errors) == error_count
        ordered = ordered_eastmoney_quotes(quotes, requested)
        if ordered is not None:
            return ordered
    if endpoint_responded:
        available = _ordered_available_eastmoney_quotes(covered_quotes, requested)
        if available:
            return available
        missing = _missing_eastmoney_symbols(covered_quotes, requested)
        raise ProviderCoverageMiss("东方财富轻量行情未覆盖请求股票：" + ",".join(missing))
    if errors:
        raise ProviderError("东方财富轻量行情不可用：" + "；".join(errors[:2]))
    return []


def eastmoney_requested_symbols(symbols) -> list[str]:
    return [eastmoney_standard_symbol(symbol) for symbol in symbols]


def eastmoney_quote_params(requested: list[str]) -> dict[str, str]:
    quote_symbols = unique_eastmoney_symbols(requested)
    return {
        "fltt": "2",
        "invt": "2",
        "ut": EASTMONEY_UT_PARAM,
        "fields": ",".join(EASTMONEY_QUOTE_FIELDS),
        "secids": ",".join(eastmoney_quote_secid(symbol) for symbol in quote_symbols),
    }


def unique_eastmoney_symbols(symbols: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = eastmoney_standard_symbol(symbol)
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def eastmoney_standard_symbol(symbol: object) -> str:
    try:
        return standard_symbol(str(symbol).strip())
    except (AttributeError, ValueError):
        raise ValueError(f"东方财富行情请求包含无效股票代码：{symbol}") from None


def eastmoney_quote_urls() -> list[str]:
    return [f"{scheme}://{host}/api/qt/ulist.np/get" for host in EASTMONEY_QUOTE_HOSTS for scheme in EASTMONEY_SCHEMES]


def eastmoney_quotes_from_url(url: str, params: dict[str, str], errors: list[str]) -> list[Quote]:
    try:
        data = eastmoney_get_json(url, params)
        return eastmoney_quotes_from_rows(eastmoney_quote_rows(data), errors)
    except Exception as exc:
        errors.append(sanitize_provider_error(exc))
        return []


def eastmoney_quote_rows(data: dict[str, Any]) -> list[Any]:
    data_block = data.get("data")
    if data_block is None:
        return []
    if not isinstance(data_block, dict):
        raise ProviderProtocolError("东方财富行情 data 字段结构异常")
    rows = data_block.get("diff")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ProviderProtocolError("东方财富行情 diff 字段结构异常")
    return rows


def eastmoney_quotes_from_rows(rows: list[Any], errors: list[str]) -> list[Quote]:
    quotes: list[Quote] = []
    for row in rows:
        if not isinstance(row, dict):
            errors.append("东方财富行情行格式异常")
            continue
        try:
            quotes.append(eastmoney_quote_from_row(row))
        except ValueError as exc:
            errors.append(sanitize_provider_error(exc))
    return quotes


def ordered_eastmoney_quotes(quotes: list[Quote], requested: list[str]) -> list[Quote] | None:
    by_symbol = {standard_symbol(f"{item.code}.{item.market}"): item for item in quotes}
    if not all(symbol in by_symbol for symbol in requested):
        return None
    return [by_symbol[symbol] for symbol in requested]


def _ordered_available_eastmoney_quotes(quotes: list[Quote], requested: list[str]) -> list[Quote]:
    by_symbol = {standard_symbol(f"{item.code}.{item.market}"): item for item in quotes}
    return [by_symbol[symbol] for symbol in requested if symbol in by_symbol]


def _missing_eastmoney_symbols(quotes: list[Quote], requested: list[str]) -> list[str]:
    covered = {standard_symbol(f"{item.code}.{item.market}") for item in quotes}
    return list(dict.fromkeys(symbol for symbol in requested if symbol not in covered))


def eastmoney_quote_from_row(row: dict[str, Any]) -> Quote:
    fields = eastmoney_quote_fields(row)
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
        timestamp=fields.timestamp,
        source=EASTMONEY_BRIDGE_SOURCE_NAME,
    )


def eastmoney_quote_fields(row: dict[str, Any]) -> EastmoneyQuoteFields:
    code = eastmoney_quote_code(row)
    price = eastmoney_required_number(row, "f2", "东方财富现价")
    prev_close = eastmoney_prev_close(row, price)
    open_price = eastmoney_number_or_default(row, "f17", price, "东方财富开盘价")
    high = eastmoney_number_or_default(row, "f15", price, "东方财富最高价")
    low = eastmoney_number_or_default(row, "f16", price, "东方财富最低价")
    ensure_quote_price_bounds(price, high, low)
    return EastmoneyQuoteFields(
        code=code,
        market=normalize_symbol(code)[1].upper(),
        name=eastmoney_quote_name(row, code),
        price=price,
        prev_close=prev_close,
        open=open_price,
        high=high,
        low=low,
        volume=eastmoney_non_negative_number(row, "f5", "东方财富成交量"),
        amount=eastmoney_non_negative_number(row, "f6", "东方财富成交额"),
        change=eastmoney_change(row, price, prev_close),
        change_pct=eastmoney_change_pct(row, price, prev_close),
        turnover_rate=eastmoney_optional_non_negative_number(row, "f8", "东方财富换手率"),
        pe=eastmoney_optional_number(row, "f9", fallback_key="f115"),
        pb=eastmoney_optional_non_negative_number(row, "f23", "东方财富市净率"),
        market_cap=eastmoney_optional_non_negative_number(row, "f20", "东方财富总市值"),
        timestamp=eastmoney_quote_timestamp(row),
    )


def eastmoney_quote_code(row: dict[str, Any]) -> str:
    raw_code = eastmoney_text(row, "f12")
    if not raw_code.isdigit() or len(raw_code) != 6 or raw_code == "000000":
        raise ValueError("东方财富行情行缺少有效6位股票代码")
    normalize_symbol(raw_code)
    return raw_code


def eastmoney_quote_name(row: dict[str, Any], code: str) -> str:
    return eastmoney_text(row, "f14") or code


def eastmoney_prev_close(row: dict[str, Any], price: float) -> float:
    return eastmoney_required_number(row, "f18", "东方财富昨收")


def eastmoney_change(row: dict[str, Any], price: float, prev_close: float) -> float:
    return eastmoney_number(row, "f4", default=price - prev_close)


def eastmoney_change_pct(row: dict[str, Any], price: float, prev_close: float) -> float:
    value = row.get("f3")
    if value not in MISSING_NUMERIC_VALUES:
        try:
            return required_float(value, "东方财富涨跌幅")
        except ValueError:
            pass
    return (price - prev_close) / prev_close * 100 if prev_close > 0 else 0.0


def eastmoney_number(row: dict[str, Any], key: str, *, default: float = 0.0) -> float:
    value = row.get(key)
    if value in MISSING_NUMERIC_VALUES:
        return default
    return safe_float(value, default=default)


def eastmoney_non_negative_number(row: dict[str, Any], key: str, field: str, *, default: float = 0.0) -> float:
    value = eastmoney_number(row, key, default=default)
    if value < 0:
        raise ValueError(f"{field}不能为负数")
    return value


def eastmoney_required_number(row: dict[str, Any], key: str, field: str) -> float:
    return required_float(row.get(key), field, positive=True)


def eastmoney_number_or_default(row: dict[str, Any], key: str, default: float, field: str) -> float:
    value = row.get(key)
    if value in MISSING_NUMERIC_VALUES:
        return default
    return required_float(value, field, positive=True)


def ensure_quote_price_bounds(price: float, high: float, low: float) -> None:
    if high < low:
        raise ValueError("东方财富行情最高价低于最低价")
    if price > high or price < low:
        raise ValueError("东方财富行情现价超出最高/最低价范围")


def eastmoney_optional_number(row: dict[str, Any], key: str, *, fallback_key: str | None = None) -> float | None:
    value = eastmoney_number(row, key)
    if value or fallback_key is None:
        return value or None
    return eastmoney_number(row, fallback_key) or None


def eastmoney_optional_non_negative_number(
    row: dict[str, Any], key: str, field: str, *, fallback_key: str | None = None
) -> float | None:
    value = eastmoney_optional_number(row, key, fallback_key=fallback_key)
    if value is not None and value < 0:
        raise ValueError(f"{field}不能为负数")
    return value


def eastmoney_text(row: dict[str, Any], key: str) -> str:
    return str(row.get(key) or "").strip()


def eastmoney_quote_timestamp(row: dict[str, Any]) -> str:
    text = eastmoney_text(row, "f86")
    timestamp = normalize_quote_event_time(text)
    if timestamp is None:
        raise ProviderProtocolError("东方财富行情缺少有效报价事件时间")
    return timestamp


def eastmoney_kline(symbol: str, period: str, limit: int) -> list[Kline]:
    ensure_positive_limit(limit)
    data = eastmoney_history_json(symbol, period=period, include_market_cap=True)
    values = _eastmoney_kline_values(data, limit)
    if not values:
        raise ProviderCoverageMiss(f"东方财富日K未覆盖请求股票：{symbol}")
    result = [item for item in (_daily_kline_from_values(item) for item in values) if item is not None]
    if not result:
        raise ProviderProtocolError("东方财富日K有效数据为空")
    return result


def eastmoney_minute_kline(symbol: str, period: str, interval: str, limit: int) -> list[MinuteKline]:
    ensure_positive_limit(limit)
    data = eastmoney_history_json(symbol, period=period, include_market_cap=False)
    values = _eastmoney_kline_values(data, limit)
    if not values:
        raise ProviderCoverageMiss(f"东方财富分钟K线未覆盖请求股票：{symbol}")
    result = [
        item
        for item in (_minute_kline_from_values(item, interval) for item in values)
        if item is not None
    ]
    if not result:
        raise ProviderProtocolError("东方财富分钟K线有效数据为空")
    return result


def _eastmoney_kline_values(data: dict[str, Any], limit: int) -> list[list[str]]:
    ensure_positive_limit(limit)
    data_block = data.get("data")
    if data_block is None:
        return []
    if not isinstance(data_block, dict):
        raise ProviderProtocolError("东方财富历史行情 data 字段结构异常")
    rows = data_block.get("klines")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ProviderProtocolError("东方财富历史行情 klines 字段结构异常")
    return [item.strip().split(",") for item in rows[-limit:] if isinstance(item, str) and item.strip()]


def _daily_kline_from_values(values: list[str]) -> Kline | None:
    if len(values) < 6:
        return None
    row = Kline(
        date=values[0],
        open=safe_float(values[1]),
        close=safe_float(values[2]),
        high=safe_float(values[3]),
        low=safe_float(values[4]),
        volume=safe_float(values[5]),
        source=EASTMONEY_BRIDGE_SOURCE_NAME,
    )
    return row if valid_kline(row) else None


def _minute_kline_from_values(values: list[str], interval: str) -> MinuteKline | None:
    if len(values) < 7:
        return None
    row = MinuteKline(
        timestamp=values[0],
        open=safe_float(values[1]),
        close=safe_float(values[2]),
        high=safe_float(values[3]),
        low=safe_float(values[4]),
        volume=safe_float(values[5]),
        amount=safe_float(values[6]) or None,
        turnover_rate=safe_float(values[10]) if len(values) > 10 else None,
        interval=interval,
        source=EASTMONEY_BRIDGE_SOURCE_NAME,
    )
    return row if row.timestamp and valid_minute_kline(row) else None


def eastmoney_history_json(symbol: str, period: str, include_market_cap: bool) -> dict[str, Any]:
    fields2 = EASTMONEY_HISTORY_FIELDS2
    if include_market_cap:
        fields2 += ",f116"
    params = {
        "fields1": EASTMONEY_HISTORY_FIELDS1,
        "fields2": fields2,
        "ut": EASTMONEY_HISTORY_UT_PARAM,
        "klt": period,
        "fqt": "1",
        "secid": eastmoney_quote_secid(symbol),
        "beg": "19700101" if period == EASTMONEY_DAILY_PERIOD else "0",
        "end": "20500101" if period == EASTMONEY_DAILY_PERIOD else "20500000",
    }
    urls = [f"https://{EASTMONEY_HIST_HOST}/api/qt/stock/kline/get"]
    errors: list[str] = []
    for url in urls:
        try:
            return eastmoney_get_json(url, params)
        except Exception as exc:
            errors.append(sanitize_provider_error(exc))
    raise ProviderError("东方财富历史行情不可用：" + "；".join(errors[:2]))


def eastmoney_quote_secid(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    market_code = "1" if market == "sh" else "0"
    return f"{market_code}.{code}"


def _eastmoney_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def _require_https_url(url: str) -> None:
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        scheme = ""
    if scheme != "https":
        raise ProviderProtocolError("东方财富行情仅允许 HTTPS 请求")


_eastmoney_no_proxy = eastmoney_no_proxy
_merge_no_proxy = merge_no_proxy
_eastmoney_get_json = eastmoney_get_json
_eastmoney_quote_secid = eastmoney_quote_secid
_eastmoney_quotes = eastmoney_quotes
_eastmoney_quote_urls = eastmoney_quote_urls
_eastmoney_quote_from_row = eastmoney_quote_from_row
_eastmoney_kline = eastmoney_kline
_eastmoney_minute_kline = eastmoney_minute_kline
_eastmoney_history_json = eastmoney_history_json
