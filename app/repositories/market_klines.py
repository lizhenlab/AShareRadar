from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.db.market_mappers import row_to_kline, row_to_minute_kline
from app.models.market import (
    DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,
    KlineAdjustmentMode,
    UNKNOWN_KLINE_DATA_VERSION,
)
from app.models.schemas import Kline, MinuteKline
from app.utils.market_data import (
    filter_valid_klines,
    filter_valid_minute_klines,
    finite_float,
    valid_non_negative_number,
    valid_ohlc,
)
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


@dataclass(frozen=True)
class _DailyKlineContract:
    adjustment_mode: KlineAdjustmentMode
    as_of: str | None
    data_version: str
    contract_version: str


DAILY_KLINE_COLUMNS = (
    "symbol",
    "adjustment_mode",
    "date",
    "open",
    "close",
    "high",
    "low",
    "volume",
    "as_of",
    "data_version",
    "contract_version",
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
    conflict_columns=("symbol", "adjustment_mode", "date"),
    lookup_columns=("symbol", "adjustment_mode"),
    order_column="date",
)
_MINUTE_SPEC = _KlineCacheSpec(
    table="kline_minute",
    columns=MINUTE_KLINE_COLUMNS,
    conflict_columns=("symbol", "interval", "timestamp"),
    lookup_columns=("symbol", "interval"),
    order_column="timestamp",
)
_KLINE_REQUIRED_FINITE_COLUMNS = ("open", "close", "high", "low", "volume")
_MINUTE_OPTIONAL_FINITE_COLUMNS = ("amount", "turnover_rate")


class MarketKlineRepositoryMixin:
    def save_klines(self, symbol: str, klines: list[Kline], source: str) -> None:
        valid_klines = filter_valid_klines(klines)
        if not valid_klines:
            return
        contract = _daily_kline_contract(valid_klines, source)
        normalized = standard_symbol(symbol)
        fetched_at = now_text()
        rows = tuple(
            (
                normalized,
                contract.adjustment_mode,
                item.date,
                item.open,
                item.close,
                item.high,
                item.low,
                item.volume,
                contract.as_of,
                contract.data_version,
                contract.contract_version,
                item.source or source,
                fetched_at,
            )
            for item in valid_klines
        )
        incoming_row_count = len({item.date for item in valid_klines})
        self._merge_or_replace_daily_kline_rows(
            normalized,
            contract,
            rows,
            incoming_row_count,
        )

    def get_klines(
        self,
        symbol: str,
        limit: int,
        max_age_seconds: int,
        adjustment_mode: KlineAdjustmentMode = DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,
    ) -> list[Kline]:
        normalized = standard_symbol(symbol)
        mode = _validated_adjustment_mode(adjustment_mode)
        rows = self._latest_kline_rows(
            _DAILY_SPEC,
            (normalized, mode),
            limit,
            max_age_seconds,
        )
        return filter_valid_klines(row_to_kline(row) for row in rows if _valid_raw_kline_row(row))

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
        return filter_valid_minute_klines(row_to_minute_kline(row) for row in rows if _valid_raw_minute_kline_row(row))

    def _save_kline_rows(self, sql: str, rows: Iterable[tuple[object, ...]]) -> None:
        with self._lock, self._connect() as conn:
            conn.executemany(sql, rows)

    def _merge_or_replace_daily_kline_rows(
        self,
        symbol: str,
        contract: _DailyKlineContract,
        rows: tuple[tuple[object, ...], ...],
        incoming_row_count: int,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stored_contracts = conn.execute(
                """
                SELECT adjustment_mode, as_of, data_version, contract_version,
                       COUNT(*) AS row_count
                FROM kline_daily
                WHERE symbol = ?
                  AND adjustment_mode = ?
                GROUP BY adjustment_mode, as_of, data_version, contract_version
                """,
                (symbol, contract.adjustment_mode),
            ).fetchall()
            if not _stored_contract_is_compatible(stored_contracts, contract):
                stored_row_count = sum(int(row["row_count"]) for row in stored_contracts)
                if incoming_row_count < stored_row_count:
                    return
                conn.execute(
                    """
                    DELETE FROM kline_daily
                    WHERE symbol = ?
                      AND adjustment_mode = ?
                    """,
                    (symbol, contract.adjustment_mode),
                )
            conn.executemany(_DAILY_INSERT_SQL, rows)

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


def _stored_contract_is_compatible(stored_contracts, contract: _DailyKlineContract) -> bool:
    if not stored_contracts:
        return True
    expected = (
        contract.adjustment_mode,
        contract.as_of,
        contract.data_version,
        contract.contract_version,
    )
    if len(stored_contracts) != 1:
        return False
    stored = stored_contracts[0]
    actual = (
        stored["adjustment_mode"],
        stored["as_of"],
        stored["data_version"],
        stored["contract_version"],
    )
    return actual == expected


def _upsert_sql(spec: _KlineCacheSpec) -> str:
    update_columns = tuple(column for column in spec.columns if column not in spec.conflict_columns)
    return (
        f"INSERT INTO {spec.table} ({_column_names(spec.columns)}) "
        f"VALUES ({_placeholders(spec.columns)}) "
        f"ON CONFLICT({_column_names(spec.conflict_columns)}) DO UPDATE SET " + _update_assignments(update_columns)
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


def _valid_raw_kline_row(row) -> bool:
    return (
        _required_finite_columns(row, _KLINE_REQUIRED_FINITE_COLUMNS)
        and valid_ohlc(row["open"], row["close"], row["high"], row["low"])
        and valid_non_negative_number(row["volume"])
    )


def _valid_raw_minute_kline_row(row) -> bool:
    return _valid_raw_kline_row(row) and all(row[column] is None or valid_non_negative_number(row[column]) for column in _MINUTE_OPTIONAL_FINITE_COLUMNS)


def _required_finite_columns(row, columns: Iterable[str]) -> bool:
    return all(finite_float(row[column]) is not None for column in columns)


def _daily_kline_contract(rows: list[Kline], source: str) -> _DailyKlineContract:
    adjustment_mode = _one_contract_value(
        (item.adjustment_mode for item in rows),
        "adjustment_mode",
    )
    mode = _validated_adjustment_mode(adjustment_mode)
    as_of = _one_contract_value(
        ((str(item.as_of).strip() if item.as_of is not None else None) for item in rows),
        "as_of",
    )
    data_version = str(_one_contract_value((item.data_version.strip() for item in rows), "data_version"))
    contract_version = str(
        _one_contract_value(
            (item.contract_version.strip() for item in rows),
            "contract_version",
        )
    )
    if not contract_version:
        raise ValueError("日K contract_version 不能为空")
    if mode == "unknown":
        if not data_version or data_version == UNKNOWN_KLINE_DATA_VERSION:
            data_version = f"legacy|{source.strip() or 'unknown'}"
    elif not as_of or not data_version or data_version == UNKNOWN_KLINE_DATA_VERSION:
        raise ValueError("已知复权日K必须声明 as_of 和 data_version")
    return _DailyKlineContract(
        adjustment_mode=mode,
        as_of=as_of,
        data_version=data_version,
        contract_version=contract_version,
    )


def _one_contract_value(values: Iterable[object], field: str):
    unique = set(values)
    if len(unique) != 1:
        raise ValueError(f"日K序列包含多个 {field}")
    return next(iter(unique))


def _validated_adjustment_mode(value: object) -> KlineAdjustmentMode:
    if value not in {"qfq", "hfq", "none", "unknown"}:
        raise ValueError(f"不支持的日K复权方式：{value}")
    return value


_DAILY_INSERT_SQL = _upsert_sql(_DAILY_SPEC)
_MINUTE_INSERT_SQL = _upsert_sql(_MINUTE_SPEC)


__all__ = ["MarketKlineRepositoryMixin"]
