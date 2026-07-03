from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.db.market_mappers import row_to_kline, row_to_minute_kline
from app.models.schemas import Kline, MinuteKline
from app.utils.market_data import filter_valid_klines, filter_valid_minute_klines
from app.utils.symbols import standard_symbol
from app.utils.time import now_text


def _column_names(columns: Iterable[str]) -> str:
    return ", ".join(columns)


def _placeholders(columns: Iterable[str]) -> str:
    return ", ".join("?" for _column in columns)


def _update_assignments(columns: Iterable[str]) -> str:
    return ", ".join(f"{column}=excluded.{column}" for column in columns)


@dataclass(frozen=True)
class _KlineCacheSpec:
    table: str
    columns: tuple[str, ...]
    conflict_columns: tuple[str, ...]
    lookup_columns: tuple[str, ...]
    order_column: str


DAILY_KLINE_COLUMNS = (
    "symbol",
    "date",
    "open",
    "close",
    "high",
    "low",
    "volume",
    "source",
    "fetched_at",
)
MINUTE_KLINE_COLUMNS = (
    "symbol",
    "interval",
    "timestamp",
    "open",
    "close",
    "high",
    "low",
    "volume",
    "amount",
    "turnover_rate",
    "source",
    "fetched_at",
)
_DAILY_SPEC = _KlineCacheSpec(
    table="kline_daily",
    columns=DAILY_KLINE_COLUMNS,
    conflict_columns=("symbol", "date"),
    lookup_columns=("symbol",),
    order_column="date",
)
_MINUTE_SPEC = _KlineCacheSpec(
    table="kline_minute",
    columns=MINUTE_KLINE_COLUMNS,
    conflict_columns=("symbol", "interval", "timestamp"),
    lookup_columns=("symbol", "interval"),
    order_column="timestamp",
)


class MarketKlineRepositoryMixin:
    def save_klines(self, symbol: str, klines: list[Kline], source: str) -> None:
        valid_klines = filter_valid_klines(klines)
        if not valid_klines:
            return
        normalized = standard_symbol(symbol)
        fetched_at = now_text()
        rows = (
            (
                normalized,
                item.date,
                item.open,
                item.close,
                item.high,
                item.low,
                item.volume,
                source,
                fetched_at,
            )
            for item in valid_klines
        )
        self._save_kline_rows(_DAILY_INSERT_SQL, rows)

    def get_klines(self, symbol: str, limit: int, max_age_seconds: int) -> list[Kline]:
        normalized = standard_symbol(symbol)
        rows = self._latest_kline_rows(_DAILY_SPEC, (normalized,), limit, max_age_seconds)
        return filter_valid_klines(row_to_kline(row) for row in rows)

    def save_minute_klines(self, symbol: str, interval: str, rows: list[MinuteKline], source: str) -> None:
        valid_rows = filter_valid_minute_klines(rows)
        if not valid_rows:
            return
        normalized = standard_symbol(symbol)
        fetched_at = now_text()
        payload = (
            (
                normalized,
                interval,
                item.timestamp,
                item.open,
                item.close,
                item.high,
                item.low,
                item.volume,
                item.amount,
                item.turnover_rate,
                source,
                fetched_at,
            )
            for item in valid_rows
        )
        self._save_kline_rows(_MINUTE_INSERT_SQL, payload)

    def get_minute_klines(self, symbol: str, interval: str, limit: int, max_age_seconds: int) -> list[MinuteKline]:
        normalized = standard_symbol(symbol)
        rows = self._latest_kline_rows(_MINUTE_SPEC, (normalized, interval), limit, max_age_seconds)
        return filter_valid_minute_klines(row_to_minute_kline(row) for row in rows)

    def _save_kline_rows(self, sql: str, rows: Iterable[tuple[object, ...]]) -> None:
        with self._lock, self._connect() as conn:
            conn.executemany(sql, rows)

    def _latest_kline_rows(
        self,
        spec: _KlineCacheSpec,
        lookup_values: tuple[object, ...],
        limit: int,
        max_age_seconds: int,
    ):
        if limit <= 0:
            return []
        window = self._time_window(max_age_seconds)
        if window is None:
            return []
        params = (*lookup_values, *window, limit)
        with self._lock, self._connect() as conn:
            return conn.execute(_latest_rows_sql(spec), params).fetchall()


def _upsert_sql(spec: _KlineCacheSpec) -> str:
    update_columns = tuple(column for column in spec.columns if column not in spec.conflict_columns)
    return (
        f"INSERT INTO {spec.table} ({_column_names(spec.columns)}) "
        f"VALUES ({_placeholders(spec.columns)}) "
        f"ON CONFLICT({_column_names(spec.conflict_columns)}) DO UPDATE SET "
        + _update_assignments(update_columns)
    )


def _latest_rows_sql(spec: _KlineCacheSpec) -> str:
    lookup_clause = " AND ".join(f"{column} = ?" for column in spec.lookup_columns)
    selected_columns = _column_names(spec.columns)
    return f"""
        SELECT {selected_columns} FROM (
            SELECT {selected_columns} FROM {spec.table}
            WHERE {lookup_clause} AND fetched_at BETWEEN ? AND ?
            ORDER BY {spec.order_column} DESC
            LIMIT ?
        )
        ORDER BY {spec.order_column} ASC
    """


_DAILY_INSERT_SQL = _upsert_sql(_DAILY_SPEC)
_MINUTE_INSERT_SQL = _upsert_sql(_MINUTE_SPEC)


__all__ = ["MarketKlineRepositoryMixin"]
