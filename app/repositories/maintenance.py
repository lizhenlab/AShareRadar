from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
from typing import Literal

from app.config import Settings
from app.repositories.base import SQLiteRepository
from app.repositories.watchlist import cap_watchlist_unread_change_counts_to_viewable


CLEANUP_DELETE_BATCH_ROWS = 1_000
CleanupLimitScope = Literal["global", "partition"]
GLOBAL_LIMIT: CleanupLimitScope = "global"
PARTITION_LIMIT: CleanupLimitScope = "partition"


@dataclass(frozen=True)
class RuntimeCleanupSpec:
    table: str
    limit_setting: str
    keep_column: str
    order_by: str
    partition_by: tuple[str, ...] = ()
    limit_scope: CleanupLimitScope = GLOBAL_LIMIT
    protected_reference: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        if self.limit_scope not in {GLOBAL_LIMIT, PARTITION_LIMIT}:
            raise ValueError(f"unsupported cleanup limit scope: {self.limit_scope}")
        if self.limit_scope == PARTITION_LIMIT and not self.partition_by:
            raise ValueError("partition cleanup requires partition columns")
        if self.limit_scope == GLOBAL_LIMIT and self.partition_by:
            raise ValueError("global cleanup cannot declare partition columns")


REGENERABLE_RUNTIME_CLEANUP_SPECS = (
    RuntimeCleanupSpec(
        "quote_history",
        "max_quote_history_rows",
        "id",
        "trade_date DESC",
        partition_by=("symbol",),
        limit_scope=PARTITION_LIMIT,
    ),
    RuntimeCleanupSpec(
        "kline_minute",
        "max_minute_kline_rows",
        "rowid",
        "timestamp DESC",
        partition_by=("symbol", "interval"),
        limit_scope=PARTITION_LIMIT,
    ),
    RuntimeCleanupSpec(
        "stock_concept",
        "max_stock_concept_rows",
        "rowid",
        "updated_at DESC, rank ASC, name ASC",
        partition_by=("symbol",),
        limit_scope=PARTITION_LIMIT,
    ),
    RuntimeCleanupSpec("cache_event", "max_cache_event_rows", "id", "created_at DESC, id DESC", limit_scope=GLOBAL_LIMIT),
    RuntimeCleanupSpec("task_run", "max_task_run_rows", "id", "id DESC", limit_scope=GLOBAL_LIMIT),
    RuntimeCleanupSpec(
        "monitor_event",
        "max_monitor_event_rows",
        "id",
        "COALESCE(last_seen_at, created_at) DESC, id DESC",
        limit_scope=GLOBAL_LIMIT,
    ),
)

USER_HISTORY_CLEANUP_SPECS = (
    # Alert consumers advance by the append-only id cursor. Retention must keep
    # the newest inserted events too, including events carrying an older market
    # timestamp because they arrived late.
    RuntimeCleanupSpec("alert_event", "max_alert_event_rows", "id", "id DESC", limit_scope=GLOBAL_LIMIT),
    RuntimeCleanupSpec(
        "advice_history",
        "max_advice_history_rows",
        "id",
        "id DESC",
        limit_scope=GLOBAL_LIMIT,
        protected_reference=("advice_review_plan", "advice_id"),
    ),
)
RUNTIME_CLEANUP_SPECS = REGENERABLE_RUNTIME_CLEANUP_SPECS + USER_HISTORY_CLEANUP_SPECS

TABLE_COUNT_NAMES = (
    "provider_status",
    "provider_capability_status",
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
    "advice_review_plan",
    "advice_review_result",
)


class RuntimeMaintenanceRepository(SQLiteRepository):
    def __init__(self, path: Path, lock: threading.RLock, *, settings: Settings) -> None:
        super().__init__(path, lock)
        self.settings = settings

    def cleanup_runtime_rows(self) -> dict[str, int]:
        return self._cleanup_specs(RUNTIME_CLEANUP_SPECS)

    def cleanup_regenerable_runtime_rows(self) -> dict[str, int]:
        return self._cleanup_specs(REGENERABLE_RUNTIME_CLEANUP_SPECS)

    def _cleanup_specs(self, specs: tuple[RuntimeCleanupSpec, ...]) -> dict[str, int]:
        removed: dict[str, int] = {}
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for spec in specs:
                limit = int(getattr(self.settings, spec.limit_setting))
                candidates = _cleanup_advice_candidates(conn, spec, limit)
                removed[spec.table] = _cleanup_table(conn, spec, limit)
                if candidates:
                    deleted_symbols = _deleted_advice_symbols(conn, candidates)
                    cap_watchlist_unread_change_counts_to_viewable(conn, deleted_symbols)
        return removed

    def preview_runtime_cleanup(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            return {
                spec.table: _cleanup_candidate_count(
                    conn,
                    spec,
                    int(getattr(self.settings, spec.limit_setting)),
                )
                for spec in RUNTIME_CLEANUP_SPECS
            }

    def table_counts(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            return {table: _table_count(conn, table) for table in TABLE_COUNT_NAMES}


def _cleanup_table(
    conn: sqlite3.Connection,
    spec: RuntimeCleanupSpec,
    limit: int,
    delete_batch_rows: int | None = None,
) -> int:
    delete_batch_rows = CLEANUP_DELETE_BATCH_ROWS if delete_batch_rows is None else delete_batch_rows
    if limit <= 0 or delete_batch_rows <= 0:
        return 0
    if spec.limit_scope == PARTITION_LIMIT:
        return sum(_cleanup_scope(conn, spec, limit, delete_batch_rows, partition_values) for partition_values in _partition_values(conn, spec))
    return _cleanup_scope(conn, spec, limit, delete_batch_rows, ())


def _cleanup_candidate_count(conn: sqlite3.Connection, spec: RuntimeCleanupSpec, limit: int) -> int:
    if limit <= 0:
        return 0
    if spec.protected_reference is not None:
        partition_values = _partition_values(conn, spec) if spec.limit_scope == PARTITION_LIMIT else [()]
        return sum(_protected_scope_candidate_count(conn, spec, limit, values) for values in partition_values)
    if spec.limit_scope == GLOBAL_LIMIT:
        count = int(conn.execute(f"SELECT COUNT(*) FROM {spec.table}").fetchone()[0])
        return max(0, count - limit)
    columns = ", ".join(spec.partition_by)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(CASE WHEN row_count > ? THEN row_count - ? ELSE 0 END), 0)
        FROM (
            SELECT COUNT(*) AS row_count
            FROM {spec.table}
            GROUP BY {columns}
        )
        """,
        (limit, limit),
    ).fetchone()
    return max(0, int(row[0]))


def _cleanup_advice_candidates(
    conn: sqlite3.Connection,
    spec: RuntimeCleanupSpec,
    limit: int,
) -> dict[int, str]:
    if spec.table != "advice_history" or limit <= 0:
        return {}
    rows = conn.execute(
        f"""
        SELECT candidate.{spec.keep_column}, candidate.symbol
        FROM ({_advice_retention_overflow_sql(spec)}) AS candidate
        {_protected_reference_where_sql(spec)}
        """,
        (limit,),
    ).fetchall()
    return {int(row[0]): str(row["symbol"]) for row in rows}


def _deleted_advice_symbols(conn: sqlite3.Connection, candidates: dict[int, str]) -> set[str]:
    remaining_ids: set[int] = set()
    candidate_ids = tuple(candidates)
    for start in range(0, len(candidate_ids), 900):
        batch = candidate_ids[start : start + 900]
        placeholders = ", ".join("?" for _ in batch)
        remaining_ids.update(
            int(row[0])
            for row in conn.execute(
                f"SELECT id FROM advice_history WHERE id IN ({placeholders})",
                batch,
            )
        )
    return {symbol for row_id, symbol in candidates.items() if row_id not in remaining_ids}


def _cleanup_scope(
    conn: sqlite3.Connection,
    spec: RuntimeCleanupSpec,
    limit: int,
    delete_batch_rows: int,
    partition_values: tuple[object, ...],
) -> int:
    removed = 0
    while _scope_exceeds_limit(conn, spec, limit, partition_values):
        params = (*partition_values, limit, delete_batch_rows) if spec.protected_reference is not None else (*partition_values, delete_batch_rows, limit)
        cursor = conn.execute(
            _cleanup_batch_sql(spec),
            params,
        )
        batch_removed = max(0, int(cursor.rowcount))
        removed += batch_removed
        if batch_removed == 0:
            break
    return removed


def _scope_exceeds_limit(
    conn: sqlite3.Connection,
    spec: RuntimeCleanupSpec,
    limit: int,
    partition_values: tuple[object, ...],
) -> bool:
    if spec.protected_reference is not None:
        row = conn.execute(
            f"""
            SELECT 1
            FROM ({_retention_overflow_sql(spec)}) AS candidate
            {_protected_reference_where_sql(spec)}
            LIMIT 1
            """,
            (*partition_values, limit),
        ).fetchone()
        return row is not None
    where_sql = _partition_where_sql(spec)
    row = conn.execute(
        f"SELECT 1 FROM {spec.table}{where_sql} LIMIT 1 OFFSET ?",
        (*partition_values, limit),
    ).fetchone()
    return row is not None


def _cleanup_batch_sql(spec: RuntimeCleanupSpec) -> str:
    if spec.protected_reference is not None:
        return f"""
            DELETE FROM {spec.table}
            WHERE {spec.keep_column} IN (
                SELECT candidate.{spec.keep_column}
                FROM ({_retention_overflow_sql(spec)}) AS candidate
                {_protected_reference_where_sql(spec)}
                LIMIT ?
            )
        """
    where_sql = _partition_where_sql(spec)
    return f"""
        DELETE FROM {spec.table}
        WHERE {spec.keep_column} IN (
            SELECT {spec.keep_column}
            FROM {spec.table}{where_sql}
            ORDER BY {spec.order_by}
            LIMIT ? OFFSET ?
        )
    """


def _protected_scope_candidate_count(
    conn: sqlite3.Connection,
    spec: RuntimeCleanupSpec,
    limit: int,
    partition_values: tuple[object, ...],
) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM ({_retention_overflow_sql(spec)}) AS candidate
        {_protected_reference_where_sql(spec)}
        """,
        (*partition_values, limit),
    ).fetchone()
    return max(0, int(row[0]))


def _retention_overflow_sql(spec: RuntimeCleanupSpec) -> str:
    return f"""
        SELECT {spec.keep_column}
        FROM {spec.table}{_partition_where_sql(spec)}
        ORDER BY {spec.order_by}
        LIMIT -1 OFFSET ?
    """


def _advice_retention_overflow_sql(spec: RuntimeCleanupSpec) -> str:
    return f"""
        SELECT {spec.keep_column}, symbol
        FROM {spec.table}{_partition_where_sql(spec)}
        ORDER BY {spec.order_by}
        LIMIT -1 OFFSET ?
    """


def _protected_reference_where_sql(spec: RuntimeCleanupSpec) -> str:
    if spec.protected_reference is None:
        return ""
    table, reference_column = spec.protected_reference
    return f"""
        WHERE NOT EXISTS (
            SELECT 1
            FROM {table} AS protected_reference
            WHERE protected_reference.{reference_column} = candidate.{spec.keep_column}
        )
    """


def _partition_values(conn: sqlite3.Connection, spec: RuntimeCleanupSpec) -> list[tuple[object, ...]]:
    columns = ", ".join(spec.partition_by)
    return [tuple(row) for row in conn.execute(f"SELECT DISTINCT {columns} FROM {spec.table}").fetchall()]


def _partition_where_sql(spec: RuntimeCleanupSpec) -> str:
    if not spec.partition_by:
        return ""
    predicates = " AND ".join(f"{column} = ?" for column in spec.partition_by)
    return f" WHERE {predicates}"


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
