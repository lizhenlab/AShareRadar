from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from math import ceil, isfinite
import re
from threading import Lock
import time
from typing import Any
from urllib.parse import urlsplit

import requests  # type: ignore[import-untyped]

from app.models.market import DAILY_KLINE_CONTRACT_VERSION, Kline
from app.services.provider_errors import (
    ProviderCoverageMiss,
    ProviderError,
    ProviderProtocolError,
    ProviderTransportError,
    sanitize_provider_error,
)
from app.utils.symbols import normalize_symbol


SINA_BJ_STOCK_POOL_SOURCE_NAME = "AKShare·新浪财经"
SINA_QFQ_DAILY_KLINE_SOURCE_NAME = "新浪财经·前复权日K"
SINA_MARKET_NODE = "hs_bjs"
SINA_STOCK_COUNT_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/" "Market_Center.getHQNodeStockCount"
SINA_STOCK_DATA_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/" "Market_Center.getHQNodeData"
SINA_DAILY_KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/" "CN_MarketDataService.getKLineData"
SINA_QFQ_URL_TEMPLATE = "https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js"
SINA_PAGE_SIZE = 100
SINA_MAX_STOCK_COUNT = 2_000
SINA_MAX_DAILY_KLINE_LIMIT = 1_970
SINA_MIN_REQUEST_INTERVAL_SECONDS = 0.12
SINA_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://vip.stock.finance.sina.com.cn/mkt/",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
}
_QFQ_ASSIGNMENT_RE = re.compile(r"\Avar\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*")
_SINA_REQUEST_LOCK = Lock()
_SINA_LAST_REQUEST_STARTED_AT: float | None = None


@dataclass(frozen=True)
class _RawDailyBar:
    day: date
    open: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    volume: Decimal


@dataclass(frozen=True)
class _QfqFactor:
    effective_on: date
    value: Decimal


def sina_qfq_daily_klines(
    symbol: str,
    *,
    limit: int = 120,
    timeout: float = 8,
) -> list[Kline]:
    """Load strictly validated Sina daily bars and apply forward-filled QFQ factors."""
    validated_limit = _validated_daily_kline_limit(limit)
    validated_timeout = _validated_timeout(timeout)
    provider_symbol = _normalized_sina_symbol(symbol)
    raw_payload = _sina_get_json(
        SINA_DAILY_KLINE_URL,
        {
            "symbol": provider_symbol,
            "scale": "240",
            "ma": "no",
            "datalen": str(validated_limit),
        },
        timeout=validated_timeout,
    )
    bars = _validated_raw_daily_bars(raw_payload, limit=validated_limit)
    factor_text = _sina_get_text(
        SINA_QFQ_URL_TEMPLATE.format(symbol=provider_symbol),
        params=None,
        timeout=validated_timeout,
    )
    factors = _validated_qfq_factors(factor_text, expected_symbol=provider_symbol)
    return _apply_qfq_factors(bars, factors)


def _validated_daily_kline_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit 必须是整数")
    if value <= 0 or value > SINA_MAX_DAILY_KLINE_LIMIT:
        raise ValueError(f"limit 必须在 1 到 {SINA_MAX_DAILY_KLINE_LIMIT} 之间")
    return value


def _validated_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout 必须是正的有限数")
    timeout = float(value)
    if not isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout 必须是正的有限数")
    return timeout


def _normalized_sina_symbol(symbol: object) -> str:
    if not isinstance(symbol, str):
        raise ValueError("新浪日K股票代码必须是字符串")
    code, market = normalize_symbol(symbol)
    _, inferred_market = normalize_symbol(code)
    if market != inferred_market:
        raise ValueError(f"股票代码 {code} 与市场标识 {market.upper()} 不一致")
    return f"{market}{code}"


def _validated_raw_daily_bars(payload: Any, *, limit: int) -> list[_RawDailyBar]:
    if payload is None or payload == []:
        raise ProviderCoverageMiss("新浪日K未覆盖请求股票或返回空序列")
    if not isinstance(payload, list):
        raise ProviderProtocolError("新浪日K返回结构异常")
    if len(payload) > limit:
        raise ProviderProtocolError(f"新浪日K返回 {len(payload)} 条，超过请求上限 {limit}")

    result: list[_RawDailyBar] = []
    previous_day: date | None = None
    for index, row in enumerate(payload, start=1):
        if not isinstance(row, dict):
            raise ProviderProtocolError(f"新浪日K第 {index} 条记录不是对象")
        day = _strict_iso_date(row.get("day"), f"新浪日K第 {index} 条日期")
        if previous_day is not None and day <= previous_day:
            raise ProviderProtocolError("新浪日K日期必须严格递增且不能重复")
        open_price = _decimal_value(row.get("open"), f"新浪日K第 {index} 条开盘价", positive=True)
        close = _decimal_value(row.get("close"), f"新浪日K第 {index} 条收盘价", positive=True)
        high = _decimal_value(row.get("high"), f"新浪日K第 {index} 条最高价", positive=True)
        low = _decimal_value(row.get("low"), f"新浪日K第 {index} 条最低价", positive=True)
        volume = _decimal_value(row.get("volume"), f"新浪日K第 {index} 条成交量", non_negative=True)
        _validate_ohlc(open_price, close, high, low, row_number=index)
        result.append(
            _RawDailyBar(
                day=day,
                open=open_price,
                close=close,
                high=high,
                low=low,
                volume=volume,
            )
        )
        previous_day = day
    return result


def _validated_qfq_factors(text: object, *, expected_symbol: str) -> list[_QfqFactor]:
    payload = _decoded_qfq_payload(text, expected_symbol=expected_symbol)
    rows = _validated_qfq_rows(payload)
    return _validated_qfq_factor_rows(rows)


def _decoded_qfq_payload(text: object, *, expected_symbol: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise ProviderProtocolError("新浪前复权因子返回空响应")
    document = text.lstrip("\ufeff \t\r\n")
    assignment = _QFQ_ASSIGNMENT_RE.match(document)
    if assignment is None:
        raise ProviderProtocolError("新浪前复权因子缺少安全的 var 赋值")
    expected_variable = f"{expected_symbol}qfq"
    if assignment.group("name") != expected_variable:
        raise ProviderProtocolError("新浪前复权因子的股票代码与请求不一致")
    try:
        payload, end = json.JSONDecoder().raw_decode(document, assignment.end())
    except json.JSONDecodeError as exc:
        raise ProviderProtocolError("新浪前复权因子不是有效 JSON") from exc
    _validate_qfq_trailer(document[end:])
    if payload is None:
        raise ProviderCoverageMiss("新浪未提供请求股票的前复权因子")
    if not isinstance(payload, dict):
        raise ProviderProtocolError("新浪前复权因子返回结构异常")
    return payload


def _validated_qfq_rows(payload: dict[str, Any]) -> list[Any]:
    total = payload.get("total")
    rows = payload.get("data")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ProviderProtocolError("新浪前复权因子 total 字段异常")
    if not isinstance(rows, list):
        raise ProviderProtocolError("新浪前复权因子 data 字段异常")
    if total != len(rows):
        raise ProviderProtocolError(f"新浪前复权因子不完整：total={total}，实际={len(rows)}")
    if not rows:
        raise ProviderCoverageMiss("新浪未提供请求股票的前复权因子")
    return rows


def _validated_qfq_factor_rows(rows: list[Any]) -> list[_QfqFactor]:
    by_day: dict[date, _QfqFactor] = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ProviderProtocolError(f"新浪前复权因子第 {index} 条记录不是对象")
        effective_on = _strict_iso_date(row.get("d"), f"新浪前复权因子第 {index} 条日期")
        value = _decimal_value(row.get("f"), f"新浪前复权因子第 {index} 条数值", positive=True)
        if effective_on in by_day:
            raise ProviderProtocolError(f"新浪前复权因子包含重复日期：{effective_on.isoformat()}")
        by_day[effective_on] = _QfqFactor(effective_on=effective_on, value=value)
    return [by_day[item] for item in sorted(by_day)]


def _validate_qfq_trailer(value: str) -> None:
    trailing = value.strip()
    if trailing.startswith(";"):
        trailing = trailing[1:].lstrip()
    if not trailing:
        return
    if not trailing.startswith("/*"):
        raise ProviderProtocolError("新浪前复权因子 JSON 后包含未知内容")
    comment_end = trailing.find("*/", 2)
    if comment_end < 0 or trailing[comment_end + 2 :].strip():
        raise ProviderProtocolError("新浪前复权因子 JSON 后包含未知内容")


def _apply_qfq_factors(bars: list[_RawDailyBar], factors: list[_QfqFactor]) -> list[Kline]:
    factor_dates = [item.effective_on for item in factors]
    as_of = bars[-1].day.isoformat()
    data_version = "|".join(
        (
            DAILY_KLINE_CONTRACT_VERSION,
            "qfq",
            SINA_QFQ_DAILY_KLINE_SOURCE_NAME,
            as_of,
        )
    )
    result: list[Kline] = []
    for bar in bars:
        factor_index = bisect_right(factor_dates, bar.day) - 1
        if factor_index < 0:
            raise ProviderCoverageMiss(f"新浪前复权因子未覆盖日K日期：{bar.day.isoformat()}")
        factor = factors[factor_index].value
        result.append(
            Kline(
                date=bar.day.isoformat(),
                open=_finite_float(bar.open / factor, "新浪前复权开盘价"),
                close=_finite_float(bar.close / factor, "新浪前复权收盘价"),
                high=_finite_float(bar.high / factor, "新浪前复权最高价"),
                low=_finite_float(bar.low / factor, "新浪前复权最低价"),
                volume=_finite_float(bar.volume, "新浪日K成交量"),
                adjustment_mode="qfq",
                as_of=as_of,
                data_version=data_version,
                source=SINA_QFQ_DAILY_KLINE_SOURCE_NAME,
            )
        )
    return result


def _strict_iso_date(value: object, label: str) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise ProviderProtocolError(f"{label}必须是 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ProviderProtocolError(f"{label}必须是有效的 YYYY-MM-DD") from None
    if value != parsed.isoformat():
        raise ProviderProtocolError(f"{label}必须是规范的 YYYY-MM-DD")
    return parsed


def _decimal_value(
    value: object,
    label: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list, tuple)):
        raise ProviderProtocolError(f"{label}不是有效数值")
    text = str(value).strip()
    if not text:
        raise ProviderProtocolError(f"{label}不是有效数值")
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ProviderProtocolError(f"{label}不是有效数值") from None
    if not parsed.is_finite():
        raise ProviderProtocolError(f"{label}必须是有限数")
    if positive and parsed <= 0:
        raise ProviderProtocolError(f"{label}必须大于 0")
    if non_negative and parsed < 0:
        raise ProviderProtocolError(f"{label}不能为负数")
    return parsed


def _validate_ohlc(
    open_price: Decimal,
    close: Decimal,
    high: Decimal,
    low: Decimal,
    *,
    row_number: int,
) -> None:
    if high < max(open_price, close) or low > min(open_price, close) or low > high:
        raise ProviderProtocolError(f"新浪日K第 {row_number} 条 OHLC 范围异常")


def _finite_float(value: Decimal, label: str) -> float:
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        raise ProviderProtocolError(f"{label}超出可用数值范围") from None
    if not isfinite(parsed):
        raise ProviderProtocolError(f"{label}超出可用数值范围")
    return parsed


def sina_bj_stock_pool_rows(*, timeout: float = 8) -> list[dict[str, Any]]:
    """Load the complete BSE equity node without relying on BSE or Eastmoney."""
    raw_count = _sina_get_json(SINA_STOCK_COUNT_URL, {"node": SINA_MARKET_NODE}, timeout=timeout)
    count = _sina_stock_count(raw_count)
    pages = ceil(count / SINA_PAGE_SIZE)
    rows: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        payload = _sina_get_json(
            SINA_STOCK_DATA_URL,
            {
                "page": str(page),
                "num": str(SINA_PAGE_SIZE),
                "sort": "symbol",
                "asc": "1",
                "node": SINA_MARKET_NODE,
                "symbol": "",
                "_s_r_a": "page",
            },
            timeout=timeout,
        )
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ProviderProtocolError("新浪北交所股票列表返回结构异常")
        rows.extend(payload)
    return _validated_sina_stock_rows(rows, expected_count=count)


def _sina_stock_count(value: Any) -> int:
    if isinstance(value, bool):
        raise ProviderProtocolError("新浪北交所股票数量异常")
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ProviderProtocolError("新浪北交所股票数量异常") from None
    if count <= 0 or count > SINA_MAX_STOCK_COUNT:
        raise ProviderProtocolError("新浪北交所股票数量超出合理范围")
    return count


def _validated_sina_stock_rows(rows: list[dict[str, Any]], *, expected_count: int) -> list[dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("code") or row.get("代码") or "").strip()
        name = str(row.get("name") or row.get("名称") or "").strip()
        if len(code) != 6 or not code.isdigit() or not name:
            raise ProviderProtocolError("新浪北交所股票列表包含无效记录")
        if code in by_code:
            raise ProviderProtocolError(f"新浪北交所股票列表包含重复代码：{code}")
        by_code[code] = row
    if len(by_code) != expected_count:
        raise ProviderProtocolError(f"新浪北交所股票列表不完整：期望 {expected_count} 条，实际 {len(by_code)} 条")
    return list(by_code.values())


def _sina_get_json(url: str, params: Mapping[str, str], *, timeout: float) -> Any:
    text = _sina_get_text(url, params=params, timeout=timeout)
    return _decode_json_document(text)


def _sina_get_text(
    url: str,
    *,
    params: Mapping[str, str] | None,
    timeout: float,
) -> str:
    return _sina_request_text(url, params=params, timeout=_validated_timeout(timeout))


def _decode_json_document(text: object) -> Any:
    if not isinstance(text, str) or not text.strip():
        raise ProviderProtocolError("新浪行情接口返回空响应")
    document = text.lstrip("\ufeff \t\r\n")
    try:
        payload, end = json.JSONDecoder().raw_decode(document)
    except json.JSONDecodeError as exc:
        raise ProviderProtocolError("新浪行情接口返回非 JSON 响应") from exc
    if document[end:].strip():
        raise ProviderProtocolError("新浪行情 JSON 后包含未知内容")
    return payload


def _sina_request_text(
    url: str,
    *,
    params: Mapping[str, str] | None,
    timeout: float,
    min_interval: float | None = None,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
    session_factory: Callable[[], Any] | None = None,
) -> str:
    _validate_https_url(url)
    validated_timeout = _validated_timeout(timeout)
    interval = SINA_MIN_REQUEST_INTERVAL_SECONDS if min_interval is None else min_interval
    if not isfinite(interval) or interval < 0:
        raise ValueError("新浪请求最小间隔必须是非负有限数")
    monotonic = clock or time.monotonic
    sleep = sleeper or time.sleep
    make_session = session_factory or requests.Session
    try:
        with _SINA_REQUEST_LOCK:
            _wait_for_sina_request_slot(interval, clock=monotonic, sleeper=sleep)
        with make_session() as session:
            response = session.get(
                url,
                params=dict(params) if params is not None else None,
                headers=SINA_HEADERS,
                timeout=validated_timeout,
            )
            response.raise_for_status()
            text = response.text
            if not isinstance(text, str):
                raise ProviderProtocolError("新浪行情接口返回的响应不是文本")
            return text
    except ProviderError:
        raise
    except requests.RequestException as exc:
        raise ProviderTransportError(sanitize_provider_error(exc)) from exc
    except Exception as exc:
        raise ProviderTransportError(sanitize_provider_error(exc)) from exc


def _wait_for_sina_request_slot(
    min_interval: float,
    *,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> None:
    global _SINA_LAST_REQUEST_STARTED_AT

    now = clock()
    if not isfinite(now):
        raise ProviderProtocolError("新浪请求节流时钟异常")
    if _SINA_LAST_REQUEST_STARTED_AT is not None:
        elapsed = max(0.0, now - _SINA_LAST_REQUEST_STARTED_AT)
        wait_seconds = max(0.0, min_interval - elapsed)
        if wait_seconds > 0:
            sleeper(wait_seconds)
            observed = clock()
            if not isfinite(observed):
                raise ProviderProtocolError("新浪请求节流时钟异常")
            now = max(observed, now + wait_seconds)
    _SINA_LAST_REQUEST_STARTED_AT = now


def _validate_https_url(url: object) -> None:
    if not isinstance(url, str):
        raise ProviderProtocolError("新浪行情接口地址必须是 HTTPS URL")
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ProviderProtocolError("新浪行情接口地址不合法") from None
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ProviderProtocolError("新浪行情接口仅允许 HTTPS")


__all__ = [
    "SINA_BJ_STOCK_POOL_SOURCE_NAME",
    "SINA_QFQ_DAILY_KLINE_SOURCE_NAME",
    "sina_bj_stock_pool_rows",
    "sina_qfq_daily_klines",
]
