from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
import time
from typing import Literal

from app.config import Settings
from app.repositories.base import SQLiteRepository
from app.repositories.market_klines import DAILY_KLINE_RETENTION_ORDER_BY, DAILY_KLINE_RETENTION_PARTITION
from app.repositories.watchlist import cap_watchlist_unread_change_counts_to_viewable


CleanupLimitScope = Literal["global", "partition"]
GLOBAL_LIMIT: CleanupLimitScope = "global"
PARTITION_LIMIT: CleanupLimitScope = "partition"
DATABASE_COMPACTION_MIN_FREE_BYTES = 8 * 1024 * 1024
DATABASE_COMPACTION_MIN_FREE_RATIO = 0.25
DATABASE_COMPACTION_BUSY_TIMEOUT_MS = 250


@dataclass(frozen=True)
class RuntimeCleanupSpec:
    table: str
    limit_setting: str
    keep_column: str
    order_by: str
    partition_by: tuple[str, ...] = ()
    limit_scope: CleanupLimitScope = GLOBAL_LIMIT
    protected_reference: tuple[str, str] | None = None
    protect_references_from_retained_only: bool = False
    protected_statuses: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.limit_scope not in {GLOBAL_LIMIT, PARTITION_LIMIT}:
            raise ValueError(f"unsupported cleanup limit scope: {self.limit_scope}")
        if self.limit_scope == PARTITION_LIMIT and not self.partition_by:
            raise ValueError("partition cleanup requires partition columns")
        if self.limit_scope == GLOBAL_LIMIT and self.partition_by:
            raise ValueError("global cleanup cannot declare partition columns")
        if self.protect_references_from_retained_only:
            if self.protected_reference is None or self.protected_reference[0] != self.table:
                raise ValueError("retained-only reference protection requires a self-reference")
            if self.limit_scope != GLOBAL_LIMIT:
                raise ValueError("retained-only reference protection requires a global limit")


REGENERABLE_RUNTIME_CLEANUP_SPECS = (
    RuntimeCleanupSpec(
        "quote_history",
        "max_quote_history_rows",
        "id",
        "trade_date DESC, id DESC",
        partition_by=("symbol",),
        limit_scope=PARTITION_LIMIT,
    ),
    RuntimeCleanupSpec(
        "kline_daily",
        "max_daily_kline_rows",
        "rowid",
        DAILY_KLINE_RETENTION_ORDER_BY,
        partition_by=DAILY_KLINE_RETENTION_PARTITION,
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
    RuntimeCleanupSpec(
        "market_scan_run",
        "max_market_scan_runs",
        "id",
        "id DESC",
        limit_scope=GLOBAL_LIMIT,
        protected_reference=("market_scan_run", "retry_of_run_id"),
        protect_references_from_retained_only=True,
        protected_statuses=("queued", "running", "cancelling"),
    ),
    RuntimeCleanupSpec(
        "task_run",
        "max_task_run_rows",
        "id",
        "id DESC",
        limit_scope=GLOBAL_LIMIT,
        protected_reference=("market_scan_run", "task_run_id"),
        protected_statuses=("running",),
    ),
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
    "market_scan_run",
    "market_scan_result",
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
        self._last_regenerable_cleanup_at: float | None = None

    def cleanup_runtime_rows(self) -> dict[str, int]:
        with self._lock:
            removed = self._cleanup_specs(RUNTIME_CLEANUP_SPECS)
            self._last_regenerable_cleanup_at = time.monotonic()
            return removed

    def cleanup_regenerable_runtime_rows(self) -> dict[str, int]:
        with self._lock:
            now = time.monotonic()
            interval = int(self.settings.runtime_maintenance_interval_seconds)
            if self._last_regenerable_cleanup_at is not None and now - self._last_regenerable_cleanup_at < interval:
                return {}
            removed = self._cleanup_specs(REGENERABLE_RUNTIME_CLEANUP_SPECS)
            self._last_regenerable_cleanup_at = time.monotonic()
            return removed

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
        if sum(removed.values()) > 0:
            self._compact_database_if_worthwhile()
        return removed

    def _compact_database_if_worthwhile(self) -> bool:
        try:
            with self._connect() as conn:
                page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
                free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
                page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
                free_bytes = free_pages * page_size
                if (
                    page_count <= 0
                    or free_bytes < DATABASE_COMPACTION_MIN_FREE_BYTES
                    or free_pages / page_count < DATABASE_COMPACTION_MIN_FREE_RATIO
                ):
                    return False
                conn.execute(f"PRAGMA busy_timeout = {DATABASE_COMPACTION_BUSY_TIMEOUT_MS}")
                conn.execute("VACUUM")
                with suppress(sqlite3.Error):
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                return True
        except sqlite3.Error:
            # Logical retention already committed. A competing reader/writer may
            # make compaction temporarily unavailable, so retry on a later pass.
            return False

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
    del delete_batch_rows
    if limit <= 0:
        return 0
    cursor = conn.execute(_cleanup_sql(spec), {"retention_limit": limit})
    return max(0, int(cursor.rowcount))


def _cleanup_candidate_count(conn: sqlite3.Connection, spec: RuntimeCleanupSpec, limit: int) -> int:
    if limit <= 0:
        return 0
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM ({_retention_overflow_sql(spec)}) AS overflow
        """,
        {"retention_limit": limit},
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
        SELECT overflow.retention_key, overflow.symbol
        FROM ({_retention_overflow_sql(spec)}) AS overflow
        """,
        {"retention_limit": limit},
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


def _cleanup_sql(spec: RuntimeCleanupSpec) -> str:
    return f"""
        DELETE FROM {spec.table}
        WHERE {spec.keep_column} IN (
            SELECT overflow.retention_key
            FROM ({_retention_overflow_sql(spec)}) AS overflow
        )
    """


def _retention_overflow_sql(spec: RuntimeCleanupSpec) -> str:
    partition = f"PARTITION BY {', '.join(spec.partition_by)} " if spec.limit_scope == PARTITION_LIMIT else ""
    return f"""
        SELECT candidate.*
        FROM (
            SELECT retained_source.{spec.keep_column} AS retention_key,
                   retained_source.*,
                   ROW_NUMBER() OVER ({partition}ORDER BY {spec.order_by}) AS retention_rank
            FROM {spec.table} AS retained_source
        ) AS candidate
        WHERE candidate.retention_rank > :retention_limit
        {_candidate_protection_sql(spec)}
    """


def _candidate_protection_sql(spec: RuntimeCleanupSpec) -> str:
    predicates: list[str] = []
    if spec.protected_reference is not None:
        table, reference_column = spec.protected_reference
        if spec.protect_references_from_retained_only:
            statuses = _quoted_statuses(spec.protected_statuses)
            retained_condition = "protected_reference.reference_retention_rank <= :retention_limit"
            if statuses:
                retained_condition += f" OR protected_reference.reference_status IN ({statuses})"
            predicates.append(
                f"""NOT EXISTS (
                SELECT 1
                FROM (
                    SELECT retained_reference.{reference_column} AS protected_key,
                           retained_reference.status AS reference_status,
                           ROW_NUMBER() OVER (ORDER BY {spec.order_by}) AS reference_retention_rank
                    FROM {table} AS retained_reference
                ) AS protected_reference
                WHERE protected_reference.protected_key = candidate.retention_key
                  AND ({retained_condition})
            )"""
            )
        else:
            predicates.append(
                f"""NOT EXISTS (
                SELECT 1
                FROM {table} AS protected_reference
                WHERE protected_reference.{reference_column} = candidate.retention_key
            )"""
            )
    if spec.protected_statuses:
        statuses = _quoted_statuses(spec.protected_statuses)
        predicates.append(f"candidate.status NOT IN ({statuses})")
    return "\n        ".join(f"AND {predicate}" for predicate in predicates)


def _quoted_statuses(statuses: tuple[str, ...]) -> str:
    return ", ".join("'" + status.replace("'", "''") + "'" for status in statuses)


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
