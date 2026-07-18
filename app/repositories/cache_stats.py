from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast
from zoneinfo import ZoneInfo

from app.models.market import DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE
from app.models.schemas import CacheStats
from app.repositories.base import SQLiteRepository


ASHARE_TIMEZONE = ZoneInfo("Asia/Shanghai")
SQLITE_MARKET_DATETIME_FUNCTION = "ashare_market_datetime"
_COMPACT_MARKET_TIME_FORMATS = {
    8: "%Y%m%d",
    12: "%Y%m%d%H%M",
    14: "%Y%m%d%H%M%S",
}


@dataclass(frozen=True)
class _CacheCounts:
    quote_count: int
    quote_history_count: int
    daily_kline_count: int
    minute_kline_count: int
    stock_count: int
    plate_rank_count: int
    concept_count: int
    provider_count: int


@dataclass(frozen=True)
class _CacheTimes:
    latest_quote_fetched_at: str | None
    latest_daily_kline_fetched_at: str | None
    latest_minute_kline_fetched_at: str | None
    latest_quote_timestamp: str | None
    latest_daily_kline_date: str | None
    latest_minute_kline_timestamp: str | None
    latest_stock_at: str | None
    latest_plate_at: str | None


class CacheStatsRepository(SQLiteRepository):
    def stats(self) -> CacheStats:
        with self._lock, self._connect() as conn:
            _register_market_datetime_function(conn)
            counts = _read_cache_counts(conn)
            times = _read_cache_times(conn)
        return CacheStats(
            path=str(self._path),
            quote_count=counts.quote_count,
            quote_history_count=counts.quote_history_count,
            kline_count=counts.daily_kline_count + counts.minute_kline_count,
            daily_kline_count=counts.daily_kline_count,
            minute_kline_count=counts.minute_kline_count,
            stock_count=counts.stock_count,
            plate_count=counts.plate_rank_count + counts.concept_count,
            provider_count=counts.provider_count,
            latest_quote_at=times.latest_quote_fetched_at,
            latest_kline_at=times.latest_daily_kline_fetched_at,
            latest_daily_kline_at=times.latest_daily_kline_fetched_at,
            latest_minute_kline_at=times.latest_minute_kline_fetched_at,
            latest_quote_fetched_at=times.latest_quote_fetched_at,
            latest_daily_kline_fetched_at=times.latest_daily_kline_fetched_at,
            latest_minute_kline_fetched_at=times.latest_minute_kline_fetched_at,
            latest_quote_timestamp=times.latest_quote_timestamp,
            latest_daily_kline_date=times.latest_daily_kline_date,
            latest_minute_kline_timestamp=times.latest_minute_kline_timestamp,
            latest_stock_at=times.latest_stock_at,
            latest_plate_at=times.latest_plate_at,
        )


def _register_market_datetime_function(conn: sqlite3.Connection) -> None:
    conn.create_function(
        SQLITE_MARKET_DATETIME_FUNCTION,
        1,
        _normalize_market_datetime,
        deterministic=True,
    )


def _read_cache_counts(conn: sqlite3.Connection) -> _CacheCounts:
    return _CacheCounts(
        quote_count=_select_count(conn, "SELECT COUNT(*) FROM quote_snapshot"),
        quote_history_count=_select_count(conn, "SELECT COUNT(*) FROM quote_history"),
        daily_kline_count=_select_count(
            conn,
            "SELECT COUNT(*) FROM kline_daily WHERE adjustment_mode = ?",
            (DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,),
        ),
        minute_kline_count=_select_count(conn, "SELECT COUNT(*) FROM kline_minute"),
        stock_count=_select_count(conn, "SELECT COUNT(*) FROM stock_master"),
        plate_rank_count=_select_count(conn, "SELECT COUNT(*) FROM plate_rank"),
        concept_count=_select_count(conn, "SELECT COUNT(*) FROM stock_concept"),
        provider_count=_select_count(conn, "SELECT COUNT(*) FROM provider_status"),
    )


def _read_cache_times(conn: sqlite3.Connection) -> _CacheTimes:
    latest_quote_fetched_at = _select_optional_text(conn, "SELECT MAX(fetched_at) FROM quote_snapshot")
    latest_daily_kline_fetched_at = _select_optional_text(
        conn,
        "SELECT MAX(fetched_at) FROM kline_daily WHERE adjustment_mode = ?",
        (DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,),
    )
    latest_minute_kline_fetched_at = _select_optional_text(conn, "SELECT MAX(fetched_at) FROM kline_minute")
    latest_quote_timestamp = _select_optional_text(
        conn,
        f"SELECT MAX({SQLITE_MARKET_DATETIME_FUNCTION}(quote_timestamp)) FROM quote_snapshot",
    )
    latest_daily_kline_datetime = _select_optional_text(
        conn,
        f"""
        SELECT MAX({SQLITE_MARKET_DATETIME_FUNCTION}(date))
        FROM kline_daily
        WHERE adjustment_mode = ?
        """,
        (DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,),
    )
    latest_minute_kline_timestamp = _select_optional_text(
        conn,
        f"SELECT MAX({SQLITE_MARKET_DATETIME_FUNCTION}(timestamp)) FROM kline_minute",
    )
    latest_stock_at = _select_optional_text(conn, "SELECT MAX(updated_at) FROM stock_master")
    latest_plate_rank_at = _select_optional_text(conn, "SELECT MAX(updated_at) FROM plate_rank")
    latest_concept_at = _select_optional_text(conn, "SELECT MAX(updated_at) FROM stock_concept")
    return _CacheTimes(
        latest_quote_fetched_at=latest_quote_fetched_at,
        latest_daily_kline_fetched_at=latest_daily_kline_fetched_at,
        latest_minute_kline_fetched_at=latest_minute_kline_fetched_at,
        latest_quote_timestamp=latest_quote_timestamp,
        latest_daily_kline_date=latest_daily_kline_datetime[:10] if latest_daily_kline_datetime is not None else None,
        latest_minute_kline_timestamp=latest_minute_kline_timestamp,
        latest_stock_at=latest_stock_at,
        latest_plate_at=max((value for value in (latest_plate_rank_at, latest_concept_at) if value), default=None),
    )


def _select_count(conn: sqlite3.Connection, query: str, parameters: tuple[object, ...] = ()) -> int:
    return cast(int, conn.execute(query, parameters).fetchone()[0])


def _select_optional_text(
    conn: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...] = (),
) -> str | None:
    return cast(str | None, conn.execute(query, parameters).fetchone()[0])


def _normalize_market_datetime(value: object) -> str | None:
    parsed = _parse_market_datetime(value)
    if parsed is None:
        return None
    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(sep=" ", timespec=timespec)


def _parse_market_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        text = str(value).strip() if value is not None else ""
        if not text:
            return None
        parsed = _parse_market_datetime_text(text)
        if parsed is None:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=None)
    return parsed.astimezone(ASHARE_TIMEZONE).replace(tzinfo=None)


def _parse_market_datetime_text(value: str) -> datetime | None:
    compact_format = _COMPACT_MARKET_TIME_FORMATS.get(len(value)) if value.isdigit() else None
    try:
        if compact_format is not None:
            return datetime.strptime(value, compact_format)
        normalized = value.replace("/", "-")
        if normalized[-1:] in {"Z", "z"}:
            normalized = f"{normalized[:-1]}+00:00"
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
