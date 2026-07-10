from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from app.models.schemas import Kline, MinuteKline, Quote, StockConceptItem, StockInfo
from app.services.data_quality_time import expected_quote_date, is_after_close, is_midday_break, is_trading_session, latest_expected_trade_date
from app.utils.symbols import standard_symbol
from app.utils.time import non_negative_seconds_since_text


MINUTE_INTERVAL_ALIASES = {
    "1": "1m",
    "1min": "1m",
    "1m": "1m",
    "3": "3m",
    "3min": "3m",
    "3m": "3m",
    "5": "5m",
    "5min": "5m",
    "5m": "5m",
    "10": "10m",
    "10min": "10m",
    "10m": "10m",
    "15": "15m",
    "15min": "15m",
    "15m": "15m",
    "30": "30m",
    "30min": "30m",
    "30m": "30m",
    "60": "60m",
    "60min": "60m",
    "60m": "60m",
    "1h": "60m",
}
SUPPORTED_MINUTE_INTERVAL_TEXT = "1m、3m、5m、10m、15m、30m、60m"


@dataclass(frozen=True)
class MinuteCacheFreshnessContext:
    latest: datetime
    current: datetime
    interval_minutes: int


@dataclass(frozen=True)
class MinuteCacheSessionRule:
    applies: Callable[[datetime], bool]
    is_fresh: Callable[[MinuteCacheFreshnessContext], bool]


def _normalize_symbols(symbols: Iterable[str]) -> list[str]:
    normalized = []
    for symbol in symbols:
        if not symbol or not symbol.strip():
            continue
        normalized.append(standard_symbol(symbol.strip()))
    return normalized


def _ordered_complete_quotes(quotes: list[Quote], requested_symbols: list[str], source_name: str) -> list[Quote]:
    by_symbol = {standard_symbol(f"{quote.code}.{quote.market}"): quote for quote in quotes}
    missing = [symbol for symbol in requested_symbols if symbol not in by_symbol]
    if missing:
        raise RuntimeError(f"{source_name} 行情缺失：{','.join(missing)}")
    return [by_symbol[symbol] for symbol in requested_symbols]


def _matched_quotes(quotes: list[Quote], requested_symbols: list[str]) -> tuple[list[Quote], list[str]]:
    by_symbol = {standard_symbol(f"{quote.code}.{quote.market}"): quote for quote in quotes}
    matched = [by_symbol[symbol] for symbol in requested_symbols if symbol in by_symbol]
    missing = [symbol for symbol in requested_symbols if symbol not in by_symbol]
    return matched, missing


def _tag_cached_quotes(quotes: list[Quote], label: str) -> list[Quote]:
    return [_quote_with_cache_label(quote, label) for quote in quotes]


def _quote_with_cache_label(quote: Quote, label: str) -> Quote:
    base_source = quote.source.split("·", 1)[0].strip() or quote.source
    return quote.model_copy(
        update={
            "source": f"{base_source}·{label}",
            "from_cache": True,
            "fallback_used": quote.fallback_used or label == "兜底缓存",
        }
    )


def _stock_pool_rows_are_authoritative(rows: list[StockInfo], min_count: int) -> bool:
    return len(rows) >= max(1, min_count)


def _stock_pool_cache_is_authoritative(
    cache,
    max_age_seconds: int,
    min_count: int,
    fresh_count: int | None = None,
) -> bool:
    stock_count = cache.stock_count if fresh_count is None else fresh_count
    return _stock_pool_cache_is_fresh(cache, max_age_seconds) and stock_count >= max(1, min_count)


def _stock_pool_cache_is_fresh(cache, max_age_seconds: int) -> bool:
    if max_age_seconds <= 0 or not cache.latest_stock_at or cache.stock_count <= 0:
        return False
    age = non_negative_seconds_since_text(cache.latest_stock_at)
    return age is not None and age <= max_age_seconds


def _kline_cache_is_fresh(klines: list[Kline]) -> bool:
    if not klines:
        return False
    last_date = _parse_kline_date(klines[-1].date)
    if last_date is None:
        return False
    current = datetime.now()
    latest_expected = latest_expected_trade_date(current)
    latest_allowed = expected_quote_date(current)
    return latest_expected <= last_date.date() <= latest_allowed


def _minute_kline_cache_is_fresh(rows: list[MinuteKline], interval: str, now: datetime | None = None) -> bool:
    current = now or datetime.now()
    context = _minute_cache_freshness_context(rows, interval, current)
    return context is not None and _minute_session_cache_is_fresh(context)


def _minute_cache_freshness_context(
    rows: list[MinuteKline],
    interval: str,
    current: datetime,
) -> MinuteCacheFreshnessContext | None:
    latest = _latest_minute_timestamp(rows)
    interval_minutes = _minute_interval_minutes(interval)
    if latest is None or interval_minutes is None:
        return None
    context = MinuteCacheFreshnessContext(latest=latest, current=current, interval_minutes=interval_minutes)
    return context if _minute_business_timestamp_is_valid(context) else None


def _latest_minute_timestamp(rows: list[MinuteKline]) -> datetime | None:
    if not rows:
        return None
    return _parse_minute_timestamp(rows[-1].timestamp)


def _minute_business_timestamp_is_valid(context: MinuteCacheFreshnessContext) -> bool:
    return context.latest <= context.current and context.latest.date() == expected_quote_date(context.current)


def _minute_session_cache_is_fresh(context: MinuteCacheFreshnessContext) -> bool:
    for rule in MINUTE_CACHE_SESSION_RULES:
        if rule.applies(context.current):
            return rule.is_fresh(context)
    return True


def _minute_trading_session_cache_is_fresh(context: MinuteCacheFreshnessContext) -> bool:
    return context.current - context.latest <= timedelta(minutes=max(10, context.interval_minutes * 3))


def _minute_midday_break_cache_is_fresh(context: MinuteCacheFreshnessContext) -> bool:
    return _is_at_or_after_time(context.latest, hour=11, minute=25)


def _minute_after_close_cache_is_fresh(context: MinuteCacheFreshnessContext) -> bool:
    return _is_at_or_after_time(context.latest, hour=14, minute=55)


MINUTE_CACHE_SESSION_RULES = (
    MinuteCacheSessionRule(is_trading_session, _minute_trading_session_cache_is_fresh),
    MinuteCacheSessionRule(is_midday_break, _minute_midday_break_cache_is_fresh),
    MinuteCacheSessionRule(is_after_close, _minute_after_close_cache_is_fresh),
)


def _parse_kline_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_minute_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value[:19])
    except ValueError:
        return None


def _minute_interval_minutes(interval: str) -> int | None:
    try:
        text = _normalize_minute_interval(interval)
        return int(text[:-1])
    except ValueError:
        return None


def _is_at_or_after_time(value: datetime, *, hour: int, minute: int) -> bool:
    return value.hour > hour or (value.hour == hour and value.minute >= minute)


def _tag_klines(
    klines: list[Kline],
    source: str | None,
    *,
    from_cache: bool,
    fallback_used: bool = False,
) -> list[Kline]:
    tagged: list[Kline] = []
    for item in klines:
        tagged.append(
            item.model_copy(
                update={
                    "source": item.source or source,
                    "from_cache": from_cache,
                    "fallback_used": fallback_used or item.fallback_used,
                }
            )
        )
    return tagged


def _tag_minute_klines(
    rows: list[MinuteKline],
    source: str | None,
    interval: str,
    *,
    from_cache: bool,
    fallback_used: bool = False,
) -> list[MinuteKline]:
    tagged: list[MinuteKline] = []
    for item in rows:
        tagged.append(
            item.model_copy(
                update={
                    "source": item.source or source,
                    "interval": interval,
                    "from_cache": from_cache,
                    "fallback_used": fallback_used or item.fallback_used,
                }
            )
        )
    return tagged


def _normalize_stock_concepts(symbol: str, rows: list[StockConceptItem], limit: int) -> list[StockConceptItem]:
    if limit <= 0:
        return []
    normalized = standard_symbol(symbol)
    deduped: dict[str, StockConceptItem] = {}
    for item in rows:
        name = item.name.strip()
        if not name or name in deduped:
            continue
        deduped[name] = item.model_copy(update={"symbol": normalized, "rank": len(deduped) + 1})
        if len(deduped) >= limit:
            break
    return list(deduped.values())


def _normalize_minute_interval(interval: str) -> str:
    normalized = str(interval or "5m").lower().strip()
    if normalized in MINUTE_INTERVAL_ALIASES:
        return MINUTE_INTERVAL_ALIASES[normalized]
    raise ValueError(f"分钟周期只支持 {SUPPORTED_MINUTE_INTERVAL_TEXT}")
