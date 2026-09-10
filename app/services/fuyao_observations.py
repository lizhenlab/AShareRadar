"""Validate auxiliary market data without manufacturing Quote freshness or scores."""

from __future__ import annotations

from datetime import date, datetime
import math
from typing import Any, cast

from app.utils.symbols import standard_a_share_stock_symbol
from app.utils.audit_time import parse_audit_time
from app.utils.clock import ASHARE_TIMEZONE, market_now
from app.services.trading_calendar import is_trading_day


VALUATION_FIELDS = ("pe_ttm", "pe_mrq", "pb_mrq", "ps_ttm", "pcf_ttm")
SENTIMENT_POOLS = ("limit-up-pool", "limit-down-pool", "limit-break-pool")


def canonical_stock(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("扶摇记录缺少股票代码")
    return standard_a_share_stock_symbol(value).upper()


def normalized_valuations(payload: dict[str, Any], symbols: list[str]) -> list[dict[str, Any]]:
    data = envelope_data(payload)
    rows = data.get("item")
    if not isinstance(rows, list):
        raise ValueError("扶摇估值列表格式异常")
    if "total" in data and (type(data["total"]) is not int or data["total"] != len(rows)):
        raise ValueError("扶摇估值总量与返回记录不一致")
    allowed, seen, result = {canonical_stock(symbol) for symbol in symbols}, set(), []
    timestamp = batch_timestamp(data.get("timestamp"))
    if len(rows) > len(allowed):
        raise ValueError("扶摇估值记录超出请求范围")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("扶摇估值记录格式异常")
        symbol = canonical_stock(row.get("thscode"))
        if symbol not in allowed or symbol in seen:
            raise ValueError("扶摇估值包含重复或非请求股票")
        seen.add(symbol)
        values = {field: finite_or_none(row.get(field)) for field in VALUATION_FIELDS}
        result.append({"symbol": symbol, "values": values, "name": safe_text(row.get("name")),
                       "batch_timestamp": timestamp, "individual_timestamp": None})
    return result


def envelope_data(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise ValueError("扶摇业务响应不可用")
    return payload["data"]


def finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ValueError("扶摇数值格式异常")
    try:
        result = float(cast(int | float, value))
    except OverflowError:
        raise ValueError("扶摇数值格式异常") from None
    if not math.isfinite(result):
        raise ValueError("扶摇数值格式异常")
    return result


def batch_timestamp(value: object) -> int | None:
    number = finite_or_none(value)
    if number is None:
        return None
    if not 0 < number < 10**13 or not number.is_integer():
        raise ValueError("扶摇批次时间格式异常")
    return int(number)


def item_rows(data: dict[str, Any], name: str = "item", limit: int = 6000) -> list[dict[str, Any]]:
    rows = data.get(name)
    if not isinstance(rows, list) or len(rows) > limit or any(not isinstance(row, dict) for row in rows):
        raise ValueError("扶摇记录列表格式或数量异常")
    return rows


def safe_text(value: object, limit: int = 500) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def normalized_pool_rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    data = envelope_data(payload)
    rows = item_rows(data, limit=200)
    pagination = pool_pagination(data, len(rows))
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("扶摇股票池记录无效")
        result.append({"symbol": canonical_stock(row.get("thscode")), "name": safe_text(row.get("name")),
                       "change_pct": finite_or_none(row.get("price_change_ratio_pct")),
                       "reason": safe_text(row.get("limit_up_reason")), "reason_kind": "provider_interpretation"})
    return result, pagination["pages"]


def pool_pagination(data: dict[str, Any], row_count: int, expected_page: int | None = None) -> dict[str, int]:
    pagination = _pool_pagination_metadata(data)
    total, pages, size, page = (pagination[key] for key in ("total", "pages", "size", "page"))
    if page > max(1, pages) or expected_page is not None and page != expected_page:
        raise ValueError("扶摇股票池响应页码不一致")
    if row_count != max(0, min(size, total - (page - 1) * size)):
        raise ValueError("扶摇股票池当前页记录不完整")
    return pagination


def _pool_pagination_metadata(data: dict[str, Any]) -> dict[str, int]:
    raw = data.get("pagination")
    if not isinstance(raw, dict) or any(type(raw.get(key)) is not int for key in ("total", "pages", "size", "page")):
        raise ValueError("扶摇股票池分页格式异常")
    total, pages, size, page = (raw[key] for key in ("total", "pages", "size", "page"))
    if not 0 <= total <= 20000 or not 1 <= size <= 200 or not 1 <= page <= 100:
        raise ValueError("扶摇股票池分页范围无效")
    expected_pages = (total + size - 1) // size
    if pages not in ({0, 1} if total == 0 else {expected_pages}) or pages > 100:
        raise ValueError("扶摇股票池页数与总量不一致")
    return {key: raw[key] for key in ("total", "pages", "size", "page")}


def valuation_history_summary(rows: list[dict[str, Any]], current: dict[str, Any]) -> dict[str, Any]:
    by_day: dict[date, tuple[datetime, dict[str, Any]]] = {}
    now = market_now()
    for row in rows:
        observation = _valuation_observation(row, now)
        if observation is None:
            continue
        instant, observed_values = observation
        day = instant.astimezone(ASHARE_TIMEZONE).date()
        if day not in by_day or instant > by_day[day][0]:
            by_day[day] = instant, observed_values
    result = {}
    for field in VALUATION_FIELDS:
        values = [value for _time, item in by_day.values() if (value := _positive_valuation(item.get(field))) is not None]
        current_values = current.get("values") if isinstance(current, dict) else None
        value = _positive_valuation(current_values.get(field)) if isinstance(current_values, dict) else None
        percentile = sum(item <= value for item in values) / len(values) * 100 if value is not None and len(values) >= 30 else None
        result[field] = {"sample_days": len(values), "percentile": percentile}
    return {"basis": "上海交易日每日至多一份本系统观察；不表示逐指标上游更新时间", "fields": result}


def _valuation_observation(row: object, now: datetime) -> tuple[datetime, dict[str, Any]] | None:
    if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
        return None
    values = row["payload"].get("values")
    if not isinstance(values, dict) or not isinstance(row.get("fetched_at"), str):
        return None
    try:
        instant = parse_audit_time(row["fetched_at"])
    except (ValueError, OverflowError):
        return None
    day = instant.astimezone(ASHARE_TIMEZONE).date()
    if instant > now or not is_trading_day(day, allow_auto_refresh=False):
        return None
    return instant, values


def _positive_valuation(value: object) -> float | None:
    try:
        number = finite_or_none(value)
    except ValueError:
        return None
    return number if number is not None and number > 0 else None
