from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3
import threading
from typing import TYPE_CHECKING

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
from app.utils.market_time import market_local_naive, market_now_naive
from app.utils.symbols import standard_symbol


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
    source: str
    fallback_used: bool
    fetched_at: str
    signatures: frozenset[tuple[str | None, str, str, str]]
    content_revision: str


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
    "fallback_used",
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
    "fallback_used",
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
DAILY_KLINE_RETENTION_PARTITION = ("symbol", "adjustment_mode")
DAILY_KLINE_RETENTION_ORDER_BY = "date DESC"
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
    if TYPE_CHECKING:
        _lock: threading.RLock

        def _connect(self) -> AbstractContextManager[sqlite3.Connection]: ...

        def _time_window(self, max_age_seconds: int) -> tuple[str, str] | None: ...

    def save_klines(self, symbol: str, klines: list[Kline], source: str) -> None:
        valid_klines = filter_valid_klines(klines)
        if not valid_klines:
            return
        normalized_klines = _normalized_daily_klines(valid_klines, source)
        fetched_at = _incoming_fetched_at(normalized_klines)
        contract = _daily_kline_contract(normalized_klines, source, fetched_at)
        normalized = standard_symbol(symbol)
        rows = tuple(
            (
                normalized,
                item.adjustment_mode,
                item.date,
                item.open,
                item.close,
                item.high,
                item.low,
                item.volume,
                item.as_of,
                item.data_version,
                item.contract_version,
                int(item.fallback_used),
                item.source or source,
                fetched_at,
            )
            for item in normalized_klines
        )
        incoming_row_count = len({item.date for item in normalized_klines})
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
        fetched_at = _incoming_fetched_at(valid_rows)
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
                int(item.fallback_used),
                item.source or source,
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
            stored_rows = conn.execute(
                f"""
                SELECT {_column_names(DAILY_KLINE_COLUMNS)}
                FROM kline_daily
                WHERE symbol = ?
                  AND adjustment_mode = ?
                ORDER BY date ASC
                """,
                (symbol, contract.adjustment_mode),
            ).fetchall()
            stored_contract = _stored_daily_kline_contract(stored_rows)
            if stored_contract is not None:
                if _daily_contract_quality_key(contract) < _daily_contract_quality_key(stored_contract):
                    return
                compatible = _stored_contract_is_compatible(stored_contract, contract)
                if not compatible and incoming_row_count < len(stored_rows):
                    return
                if not compatible:
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
        window = _market_time_window(max_age_seconds)
        if window is None:
            return []
        params = (*lookup_values, *window, limit)
        with self._lock, self._connect() as conn:
            return conn.execute(_latest_rows_sql(spec), params).fetchall()


def _stored_contract_is_compatible(
    stored: _DailyKlineContract,
    incoming: _DailyKlineContract,
) -> bool:
    return (
        stored.adjustment_mode == incoming.adjustment_mode
        and stored.source == incoming.source
        and stored.signatures == incoming.signatures
    )


def _stored_daily_kline_contract(rows) -> _DailyKlineContract | None:
    if not rows:
        return None
    klines = [row_to_kline(row) for row in rows]
    parsed_fetched_at = [
        parsed
        for row in rows
        if (parsed := _contract_as_of(str(row["fetched_at"] or ""))) is not None
    ]
    fetched_at = _datetime_text(max(parsed_fetched_at, default=datetime.min))
    return _daily_kline_contract(klines, klines[-1].source or "unknown", fetched_at)


def _daily_contract_quality_key(
    contract: _DailyKlineContract,
) -> tuple[datetime, int, datetime, str]:
    as_of = _contract_as_of(contract.as_of) or datetime.min
    fetched_at = _required_contract_datetime(contract.fetched_at, "fetched_at")
    return as_of, int(not contract.fallback_used), fetched_at, contract.content_revision


def _market_time_window(max_age_seconds: int) -> tuple[str, str] | None:
    if max_age_seconds <= 0:
        return None
    current = market_now_naive()
    cutoff = current - timedelta(seconds=max_age_seconds)
    return _datetime_text(cutoff), _datetime_text(current)


def _contract_as_of(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = market_local_naive(parsed)
    return parsed


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


def _normalized_daily_klines(rows: list[Kline], source: str) -> list[Kline]:
    adjustment_mode = _one_contract_value(
        (item.adjustment_mode for item in rows),
        "adjustment_mode",
    )
    mode = _validated_adjustment_mode(adjustment_mode)
    normalized: list[Kline] = []
    for item in rows:
        resolved_source = str(item.source or source).strip()
        if not resolved_source:
            raise ValueError("日K source 不能为空")
        data_version = item.data_version.strip()
        if mode == "unknown" and (not data_version or data_version == UNKNOWN_KLINE_DATA_VERSION):
            data_version = f"legacy|{resolved_source}"
        normalized.append(
            item.model_copy(
                update={
                    "data_version": data_version,
                    "source": resolved_source,
                }
            )
        )
    return normalized


def _daily_kline_contract(
    rows: list[Kline],
    source: str,
    fetched_at: str,
) -> _DailyKlineContract:
    mode = _daily_adjustment_mode(rows)
    contract_version = _uniform_daily_text(
        (item.contract_version for item in rows),
        "contract_version",
    )
    resolved_source = _uniform_daily_text(
        (item.source or source for item in rows),
        "source",
    )
    ordered = _ordered_daily_contract_rows(rows, mode)
    latest = ordered[-1]
    _required_contract_datetime(fetched_at, "fetched_at")
    return _DailyKlineContract(
        adjustment_mode=mode,
        as_of=str(latest.as_of).strip() if latest.as_of is not None else None,
        data_version=latest.data_version.strip(),
        contract_version=contract_version,
        source=resolved_source,
        fallback_used=any(item.fallback_used for item in rows),
        fetched_at=fetched_at,
        signatures=_daily_contract_signatures(rows, source),
        content_revision=_daily_content_revision(rows),
    )


def _ordered_daily_contract_rows(
    rows: list[Kline],
    mode: KlineAdjustmentMode,
) -> list[Kline]:
    snapshot_revisions = {
        (
            str(item.as_of).strip() if item.as_of is not None else None,
            item.data_version.strip(),
        )
        for item in rows
    }
    if len(snapshot_revisions) == 1:
        _validate_uniform_daily_snapshot(rows, mode)
        return rows
    ordered = sorted(
        rows,
        key=lambda item: _required_contract_datetime(item.date, "date"),
    )
    _validate_daily_revision_chain(ordered, mode)
    return ordered


def _daily_contract_signatures(
    rows: list[Kline],
    source: str,
) -> frozenset[tuple[str | None, str, str, str]]:
    return frozenset(
        (
            str(item.as_of).strip() if item.as_of is not None else None,
            item.data_version.strip(),
            item.contract_version.strip(),
            str(item.source or source).strip(),
        )
        for item in rows
    )


def _daily_adjustment_mode(rows: list[Kline]) -> KlineAdjustmentMode:
    return _validated_adjustment_mode(
        _one_contract_value(
            (item.adjustment_mode for item in rows),
            "adjustment_mode",
        )
    )


def _uniform_daily_text(values: Iterable[object], field: str) -> str:
    value = str(_one_contract_value((str(item or "").strip() for item in values), field))
    if not value:
        raise ValueError(f"日K {field} 不能为空")
    return value


def _validate_daily_revision_chain(
    rows: list[Kline],
    mode: KlineAdjustmentMode,
) -> None:
    previous_as_of: datetime | None = None
    for item in rows:
        data_version = item.data_version.strip()
        if not data_version or (mode != "unknown" and data_version == UNKNOWN_KLINE_DATA_VERSION):
            raise ValueError("已知复权日K必须声明 data_version")
        if mode == "unknown" and item.as_of is None:
            continue
        as_of = _required_contract_datetime(item.as_of, "as_of")
        row_date = _required_contract_datetime(item.date, "date")
        if as_of < row_date:
            raise ValueError("日K as_of 早于对应行情日期")
        if previous_as_of is not None and as_of < previous_as_of:
            raise ValueError("日K revision 链随行情日期倒退")
        previous_as_of = as_of


def _validate_uniform_daily_snapshot(
    rows: list[Kline],
    mode: KlineAdjustmentMode,
) -> None:
    item = rows[0]
    data_version = item.data_version.strip()
    if not data_version or (mode != "unknown" and data_version == UNKNOWN_KLINE_DATA_VERSION):
        raise ValueError("已知复权日K必须声明 data_version")
    if mode != "unknown" or item.as_of is not None:
        _required_contract_datetime(item.as_of, "as_of")


def _incoming_fetched_at(rows: Iterable[object]) -> str:
    values: list[str] = []
    missing_or_invalid = False
    row_count = 0
    for item in rows:
        row_count += 1
        value = str(getattr(item, "fetched_at", "") or "").strip()
        if not value or _contract_as_of(value) is None:
            missing_or_invalid = True
            continue
        values.append(value)
    if row_count == 0:
        raise ValueError("K线写入不能为空")
    if missing_or_invalid or not values:
        values.append(_datetime_text(market_now_naive()))
    latest = max(
        (_required_contract_datetime(value, "fetched_at") for value in values),
    )
    return _datetime_text(latest)


def _daily_content_revision(rows: list[Kline]) -> str:
    payload = [
        [
            item.date,
            float(item.open),
            float(item.close),
            float(item.high),
            float(item.low),
            float(item.volume),
            item.adjustment_mode,
            str(item.as_of or ""),
            item.contract_version,
            bool(item.fallback_used),
        ]
        for item in sorted(rows, key=lambda row: row.date)
    ]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _required_contract_datetime(value: object, field: str) -> datetime:
    parsed = _contract_as_of(str(value or ""))
    if parsed is None:
        raise ValueError(f"日K {field} 无法解析")
    return parsed


def _datetime_text(value: datetime) -> str:
    return value.isoformat(sep=" ", timespec="microseconds")


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
