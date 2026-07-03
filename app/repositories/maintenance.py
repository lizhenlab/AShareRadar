from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from app.config import get_settings
from app.repositories.base import SQLiteRepository


@dataclass(frozen=True)
class RuntimeCleanupSpec:
    table: str
    limit_setting: str
    keep_column: str
    order_by: str
    partition_by: tuple[str, ...] = ()


RUNTIME_CLEANUP_SPECS = (
    RuntimeCleanupSpec(
        "quote_history",
        "max_quote_history_rows",
        "id",
        "COALESCE(NULLIF(trade_date, ''), quote_timestamp, fetched_at) DESC, fetched_at DESC, id DESC",
        ("symbol",),
    ),
    RuntimeCleanupSpec(
        "kline_minute",
        "max_minute_kline_rows",
        "rowid",
        "timestamp DESC, fetched_at DESC",
        ("symbol", "interval"),
    ),
    RuntimeCleanupSpec(
        "stock_concept",
        "max_stock_concept_rows",
        "rowid",
        "updated_at DESC, rank ASC, name ASC",
        ("symbol",),
    ),
    RuntimeCleanupSpec("cache_event", "max_cache_event_rows", "id", "created_at DESC, id DESC"),
    RuntimeCleanupSpec("task_run", "max_task_run_rows", "id", "id DESC"),
    RuntimeCleanupSpec("monitor_event", "max_monitor_event_rows", "id", "COALESCE(last_seen_at, created_at) DESC, id DESC"),
    RuntimeCleanupSpec("alert_event", "max_alert_event_rows", "id", "created_at DESC, id DESC"),
    RuntimeCleanupSpec("advice_history", "max_advice_history_rows", "id", "id DESC"),
)

TABLE_COUNT_NAMES = (
    "provider_status",
    "quote_snapshot",
    "quote_history",
    "kline_daily",
    "kline_minute",
    "cache_event",
    "stock_master",
    "plate_rank",
    "stock_concept",
    "task_run",
    "monitor_event",
    "watchlist",
    "advice_history",
    "alert_rule",
    "alert_event",
    "stock_note",
)


class RuntimeMaintenanceRepository(SQLiteRepository):
    def cleanup_runtime_rows(self) -> dict[str, int]:
        settings = get_settings()
        removed: dict[str, int] = {}
        with self._lock, self._connect() as conn:
            for spec in RUNTIME_CLEANUP_SPECS:
                removed[spec.table] = _cleanup_table(conn, spec, int(getattr(settings, spec.limit_setting)))
        return removed

    def table_counts(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            return {table: _table_count(conn, table) for table in TABLE_COUNT_NAMES}


def _cleanup_table(conn: sqlite3.Connection, spec: RuntimeCleanupSpec, limit: int) -> int:
    if limit <= 0:
        return 0
    before = _table_count(conn, spec.table)
    conn.execute(_cleanup_sql(spec), (limit,))
    return before - _table_count(conn, spec.table)


def _cleanup_sql(spec: RuntimeCleanupSpec) -> str:
    if spec.partition_by:
        return _partition_cleanup_sql(spec)
    return f"""
        DELETE FROM {spec.table}
        WHERE {spec.keep_column} NOT IN (
            SELECT {spec.keep_column} FROM {spec.table}
            ORDER BY {spec.order_by}
            LIMIT ?
        )
    """


def _partition_cleanup_sql(spec: RuntimeCleanupSpec) -> str:
    partition_by = ", ".join(spec.partition_by)
    return f"""
        DELETE FROM {spec.table}
        WHERE {spec.keep_column} IN (
            SELECT keep_value
            FROM (
                SELECT
                    {spec.keep_column} AS keep_value,
                    ROW_NUMBER() OVER (
                        PARTITION BY {partition_by}
                        ORDER BY {spec.order_by}
                    ) AS keep_rank
                FROM {spec.table}
            )
            WHERE keep_rank > ?
        )
    """


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
