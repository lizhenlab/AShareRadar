from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
import sqlite3
import time

from app.db.schema_definitions import (
    KLINE_DAILY_COLUMN_DEFINITIONS,
    QUOTE_HISTORY_COLUMN_DEFINITIONS,
    SCHEMA_MIGRATION_APPLIED_AT_DEFAULT_SQL,
)
from app.utils.audit_time import normalize_audit_time_text


SCHEMA_MIGRATION_SQL = f"""
CREATE TABLE IF NOT EXISTS schema_migration (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT {SCHEMA_MIGRATION_APPLIED_AT_DEFAULT_SQL}
)
"""

MIGRATION_BUSY_TIMEOUT_MS = 15_000
JOURNAL_MODE_RETRY_COUNT = 5
QUOTE_HISTORY_UNIQUE_INDEX = "uq_quote_history_symbol_trade_date"
QUOTE_HISTORY_CONTRACT_MIGRATION = "20260715_quote_history_not_null_contract"
KLINE_DAILY_CONTRACT_MIGRATION = "20260716_kline_daily_adjustment_contract"
AUDIT_TIMESTAMP_UTC_MIGRATION = "20260724_audit_timestamps_utc_v2"
ADVICE_REVIEW_AUDIT_UTC_SCHEMA_VERSION = "20260724_advice_review_audit_timestamps_utc_v2"
SCHEMA_MIGRATION_APPLIED_AT_UTC_MIGRATION = "20260724_schema_migration_applied_at_utc_v2"
DEFAULT_LEGACY_AUDIT_TIMEZONE = "Asia/Shanghai"
_AUDIT_NORMALIZER_FUNCTION = "_ashare_normalize_audit_timestamp"
_SCHEMA_MIGRATION_NORMALIZER_FUNCTION = "_ashare_normalize_schema_migration_timestamp"
_SCHEMA_MIGRATION_REBUILD_TABLE = "schema_migration__utc_rebuild"
_QUOTE_HISTORY_REBUILD_TABLE = "quote_history__compat_rebuild"
_KLINE_DAILY_REBUILD_TABLE = "kline_daily__compat_rebuild"
_QUOTE_HISTORY_COLUMNS = (
    "id",
    "symbol",
    "code",
    "market",
    "name",
    "price",
    "change_pct",
    "pe",
    "pb",
    "market_cap",
    "fallback_used",
    "source",
    "quote_timestamp",
    "trade_date",
    "fetched_at",
)
_QUOTE_HISTORY_MIGRATION_NAMES = (
    "20260612_quote_history_trade_date",
    "20260714_quote_history_normalize_trade_date",
    "20260714_quote_history_daily_snapshot",
    QUOTE_HISTORY_CONTRACT_MIGRATION,
)
_OBSOLETE_QUOTE_HISTORY_INDEXES = (
    "idx_quote_history_symbol_time",
    "idx_quote_history_symbol_trade_date_time",
    "idx_quote_history_symbol_trade_latest",
)
_KLINE_DAILY_BASE_COLUMNS = (
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
_KLINE_DAILY_CONTRACT_COLUMNS = (
    "adjustment_mode",
    "as_of",
    "data_version",
    "contract_version",
)
_KLINE_DAILY_COLUMNS = (
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

AUDIT_TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    "provider_status": ("last_success", "updated_at"),
    "provider_capability_status": ("last_success", "updated_at"),
    "quote_snapshot": ("fetched_at",),
    "quote_history": ("fetched_at",),
    "kline_daily": ("fetched_at",),
    "kline_minute": ("fetched_at",),
    "cache_event": ("created_at",),
    "task_run": ("started_at", "finished_at"),
    "reliability_bucket": ("bucket_start_utc", "updated_at"),
    "market_scan_run": (
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "cancel_requested_at",
    ),
    "market_scan_result": ("updated_at",),
    "monitor_event": ("created_at", "last_seen_at"),
    "watchlist": ("created_at", "updated_at", "last_viewed_at"),
    "advice_history": ("created_at", "updated_at"),
    "alert_rule": (
        "last_checked_at",
        "last_triggered_at",
        "created_at",
        "updated_at",
    ),
    "alert_event": ("created_at",),
    "stock_note": ("created_at", "updated_at"),
    "stock_master": ("updated_at",),
    "plate_rank": ("updated_at",),
    "stock_concept": ("updated_at",),
    "advice_review_plan": ("created_at", "updated_at"),
    "advice_review_result": ("evaluated_at",),
}

COMPAT_COLUMNS = {
    "advice_history": {
        "updated_at": "TEXT",
        "repeat_count": "INTEGER NOT NULL DEFAULT 1",
        "snapshot_contract_version": "TEXT NOT NULL DEFAULT 'legacy'",
        "conclusion_basis": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
        "rule_version": "TEXT NOT NULL DEFAULT 'unknown'",
        "model_version": "TEXT NOT NULL DEFAULT 'unknown'",
        "market_time": "TEXT",
        "data_quality_source": "TEXT",
        "kline_adjustment_mode": "TEXT NOT NULL DEFAULT 'unknown'",
        "kline_anchor_date": "TEXT",
        "kline_anchor_close": "REAL",
        "kline_data_version": "TEXT NOT NULL DEFAULT 'unknown'",
        "kline_contract_version": "TEXT NOT NULL DEFAULT 'unknown'",
    },
    "quote_history": {
        "pe": "REAL",
        "pb": "REAL",
        "market_cap": "REAL",
        "trade_date": "TEXT NOT NULL DEFAULT ''",
        "fallback_used": "INTEGER NOT NULL DEFAULT 0 CHECK (fallback_used IN (0, 1))",
    },
    "quote_snapshot": {
        "fallback_used": "INTEGER NOT NULL DEFAULT 0 CHECK (fallback_used IN (0, 1))",
    },
    "kline_daily": {
        "fallback_used": "INTEGER NOT NULL DEFAULT 0 CHECK (fallback_used IN (0, 1))",
    },
    "kline_minute": {
        "fallback_used": "INTEGER NOT NULL DEFAULT 0 CHECK (fallback_used IN (0, 1))",
    },
    "monitor_event": {
        "last_seen_at": "TEXT",
        "repeat_count": "INTEGER NOT NULL DEFAULT 1",
    },
    "alert_rule": {
        "stock_name": "TEXT NOT NULL DEFAULT ''",
        "last_checked_at": "TEXT",
        "last_triggered_at": "TEXT",
        "last_state": "TEXT NOT NULL DEFAULT '等待'",
        "trigger_count": "INTEGER NOT NULL DEFAULT 0",
        "cooldown_seconds": "INTEGER NOT NULL DEFAULT 300",
    },
    "alert_event": {
        "stock_name": "TEXT NOT NULL DEFAULT ''",
        "event_type": "TEXT NOT NULL DEFAULT '触发'",
    },
    "stock_note": {
        "visible": "INTEGER NOT NULL DEFAULT 1",
    },
    "market_scan_result": {
        "metadata_source": "TEXT",
        "quote_fallback_used": "INTEGER NOT NULL DEFAULT 0 CHECK (quote_fallback_used IN (0, 1))",
        "kline_fallback_used": "INTEGER NOT NULL DEFAULT 0 CHECK (kline_fallback_used IN (0, 1))",
        "metadata_degraded": "INTEGER NOT NULL DEFAULT 0 CHECK (metadata_degraded IN (0, 1))",
        "degradation_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
    },
    "market_scan_run": {
        "retry_of_run_id": "INTEGER REFERENCES market_scan_run(id) ON DELETE SET NULL",
        "stock_pool_source": "TEXT",
    },
    "stock_concept": {
        "match_reason": "TEXT NOT NULL DEFAULT '概念成分匹配'",
    },
    "watchlist": {
        "research_status": ("TEXT NOT NULL DEFAULT 'watching' " "CHECK (research_status IN ('to_research', 'watching', 'holding_research', 'excluded'))"),
        "priority": "TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('high', 'medium', 'low'))",
        "next_review_date": "TEXT",
        "last_viewed_at": "TEXT",
        "unread_change_count": "INTEGER NOT NULL DEFAULT 0 CHECK (unread_change_count >= 0)",
    },
}


def apply_compat_schema(
    conn: sqlite3.Connection,
    *,
    legacy_audit_timezone: str = DEFAULT_LEGACY_AUDIT_TIMEZONE,
) -> None:
    if not conn.in_transaction:
        _ensure_wal_mode(conn)
    with migration_transaction(conn):
        ensure_migration_table(conn)
        for table, columns in COMPAT_COLUMNS.items():
            if not table_exists(conn, table):
                continue
            for column, definition in columns.items():
                ensure_column(conn, table, column, definition)
        _apply_compat_migrations(conn, legacy_audit_timezone=legacy_audit_timezone)
        ensure_compat_indexes(conn)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        return
    if any(_pragma_column_name(row) == column for row in rows):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def apply_compat_migrations(
    conn: sqlite3.Connection,
    *,
    legacy_audit_timezone: str = DEFAULT_LEGACY_AUDIT_TIMEZONE,
) -> None:
    with migration_transaction(conn):
        _apply_compat_migrations(conn, legacy_audit_timezone=legacy_audit_timezone)


def _apply_compat_migrations(
    conn: sqlite3.Connection,
    *,
    legacy_audit_timezone: str,
) -> None:
    ensure_migration_table(conn)
    _apply_schema_migration_applied_at_utc_migration(conn)
    _apply_kline_daily_migration(conn)
    _apply_quote_history_migrations(conn)
    _apply_monitor_event_migration(conn)
    _apply_market_scan_degradation_migration(conn)
    _apply_audit_timestamp_utc_migration(
        conn,
        legacy_audit_timezone=legacy_audit_timezone,
    )


def _apply_schema_migration_applied_at_utc_migration(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT 1 FROM schema_migration WHERE name = ?",
        (SCHEMA_MIGRATION_APPLIED_AT_UTC_MIGRATION,),
    ).fetchone()
    if existing is not None:
        return
    conn.create_function(
        _SCHEMA_MIGRATION_NORMALIZER_FUNCTION,
        1,
        _normalize_schema_migration_timestamp,
        deterministic=True,
    )
    conn.execute(f"DROP TABLE IF EXISTS {_SCHEMA_MIGRATION_REBUILD_TABLE}")
    conn.execute(
        f"""
        CREATE TABLE {_SCHEMA_MIGRATION_REBUILD_TABLE} (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT {SCHEMA_MIGRATION_APPLIED_AT_DEFAULT_SQL}
        )
        """
    )
    conn.execute(
        f"""
        INSERT INTO {_SCHEMA_MIGRATION_REBUILD_TABLE} (name, applied_at)
        SELECT name, {_SCHEMA_MIGRATION_NORMALIZER_FUNCTION}(applied_at)
        FROM schema_migration
        """
    )
    conn.execute("DROP TABLE schema_migration")
    conn.execute(
        f"ALTER TABLE {_SCHEMA_MIGRATION_REBUILD_TABLE} RENAME TO schema_migration"
    )
    conn.execute(
        "INSERT INTO schema_migration (name) VALUES (?)",
        (SCHEMA_MIGRATION_APPLIED_AT_UTC_MIGRATION,),
    )


def _normalize_schema_migration_timestamp(value: object) -> str:
    return normalize_audit_time_text(str(value or ""), legacy_timezone=UTC)


def _apply_audit_timestamp_utc_migration(
    conn: sqlite3.Connection,
    *,
    legacy_audit_timezone: str,
) -> None:
    existing = conn.execute(
        "SELECT 1 FROM schema_migration WHERE name = ?",
        (AUDIT_TIMESTAMP_UTC_MIGRATION,),
    ).fetchone()
    if existing is not None:
        return
    normalize_legacy_audit_timestamps(
        conn,
        AUDIT_TIMESTAMP_COLUMNS,
        legacy_audit_timezone=legacy_audit_timezone,
    )
    conn.execute(
        "INSERT INTO schema_migration (name) VALUES (?)",
        (AUDIT_TIMESTAMP_UTC_MIGRATION,),
    )


def normalize_legacy_audit_timestamps(
    conn: sqlite3.Connection,
    columns_by_table: dict[str, tuple[str, ...]],
    *,
    legacy_audit_timezone: str = DEFAULT_LEGACY_AUDIT_TIMEZONE,
) -> None:
    conn.create_function(
        _AUDIT_NORMALIZER_FUNCTION,
        1,
        _audit_timestamp_normalizer(legacy_audit_timezone),
        deterministic=True,
    )
    for table, columns in columns_by_table.items():
        existing_columns = {
            _pragma_column_name(row)
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column in columns:
            if column not in existing_columns:
                continue
            try:
                conn.execute(
                    f"""
                    UPDATE {table}
                    SET {column} = {_AUDIT_NORMALIZER_FUNCTION}({column})
                    WHERE {column} IS NOT NULL
                    """
                )
            except sqlite3.OperationalError as exc:
                if "user-defined function" not in str(exc).lower():
                    raise
                raise ValueError(
                    f"{table}.{column} contains an invalid audit timestamp"
                ) from exc


def _audit_timestamp_normalizer(legacy_audit_timezone: str):
    def normalize(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("audit timestamp must be text")
        return normalize_audit_time_text(
            value,
            legacy_timezone=legacy_audit_timezone,
        )

    return normalize


def _apply_market_scan_degradation_migration(conn: sqlite3.Connection) -> None:
    if not table_has_columns(
        conn,
        "market_scan_result",
        "tags_json",
        "list_date",
        "quote_fallback_used",
        "kline_fallback_used",
        "metadata_degraded",
        "degradation_reasons_json",
    ):
        return
    _run_once(
        conn,
        "20260719_market_scan_structured_degradation",
        """
            UPDATE market_scan_result
            SET
                quote_fallback_used = CASE
                    WHEN tags_json LIKE '%"兜底行情"%' THEN 1
                    ELSE quote_fallback_used
                END,
                kline_fallback_used = CASE
                    WHEN tags_json LIKE '%"兜底K线"%' THEN 1
                    ELSE kline_fallback_used
                END,
                metadata_degraded = CASE
                    WHEN list_date IS NULL OR tags_json LIKE '%"上市日期未知"%' THEN 1
                    ELSE metadata_degraded
                END,
                degradation_reasons_json = CASE
                    WHEN degradation_reasons_json IS NULL OR trim(degradation_reasons_json) = ''
                    THEN '[]'
                    ELSE degradation_reasons_json
                END
        """,
    )


def _apply_kline_daily_migration(conn: sqlite3.Connection) -> None:
    if not table_has_columns(conn, "kline_daily", *_KLINE_DAILY_BASE_COLUMNS):
        return
    if _kline_daily_requires_rebuild(conn):
        _rebuild_kline_daily(conn)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migration (name) VALUES (?)",
        (KLINE_DAILY_CONTRACT_MIGRATION,),
    )


def _kline_daily_requires_rebuild(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA table_info(kline_daily)").fetchall()
    columns = {_pragma_column_name(row) for row in rows}
    if not set(_KLINE_DAILY_COLUMNS).issubset(columns):
        return True
    primary_key = tuple(
        _pragma_column_name(row) for row in sorted(rows, key=_pragma_column_primary_key_position) if _pragma_column_primary_key_position(row) > 0
    )
    return primary_key != ("symbol", "adjustment_mode", "date")


def _rebuild_kline_daily(conn: sqlite3.Connection) -> None:
    existing = {_pragma_column_name(row) for row in conn.execute("PRAGMA table_info(kline_daily)").fetchall()}
    contract_expressions = {
        "adjustment_mode": _kline_adjustment_expression(existing),
        "as_of": "NULLIF(trim(as_of), '')" if "as_of" in existing else "NULL",
        "data_version": ("COALESCE(NULLIF(trim(data_version), ''), 'legacy')" if "data_version" in existing else "'legacy'"),
        "contract_version": ("COALESCE(NULLIF(trim(contract_version), ''), 'legacy')" if "contract_version" in existing else "'legacy'"),
        "fallback_used": _fallback_used_expression(existing),
    }
    select_columns = ", ".join(contract_expressions.get(column, column) for column in _KLINE_DAILY_COLUMNS)
    insert_columns = ", ".join(_KLINE_DAILY_COLUMNS)
    conn.execute(f"DROP TABLE IF EXISTS {_KLINE_DAILY_REBUILD_TABLE}")
    conn.execute(f"CREATE TABLE {_KLINE_DAILY_REBUILD_TABLE} ({KLINE_DAILY_COLUMN_DEFINITIONS})")
    conn.execute(
        f"""
        INSERT INTO {_KLINE_DAILY_REBUILD_TABLE} ({insert_columns})
        SELECT {select_columns}
        FROM kline_daily
        """
    )
    conn.execute("DROP TABLE kline_daily")
    conn.execute(f"ALTER TABLE {_KLINE_DAILY_REBUILD_TABLE} RENAME TO kline_daily")


def _kline_adjustment_expression(existing: set[str]) -> str:
    if "adjustment_mode" not in existing:
        return "'unknown'"
    return "CASE WHEN adjustment_mode IN ('qfq', 'hfq', 'none', 'unknown') " "THEN adjustment_mode ELSE 'unknown' END"


def _apply_quote_history_migrations(conn: sqlite3.Connection) -> None:
    if table_has_columns(conn, "quote_history", *_QUOTE_HISTORY_COLUMNS):
        _ensure_quote_history_contract(conn)
        return
    if table_has_columns(conn, "quote_history", "trade_date", "quote_timestamp", "fetched_at"):
        _apply_legacy_quote_history_dates(conn)
    if table_has_columns(
        conn,
        "quote_history",
        "id",
        "symbol",
        "trade_date",
        "quote_timestamp",
    ):
        _apply_legacy_quote_history_deduplication(conn)


def _apply_legacy_quote_history_dates(conn: sqlite3.Connection) -> None:
    _run_once(
        conn,
        "20260612_quote_history_trade_date",
        """
            UPDATE quote_history
            SET trade_date = substr(COALESCE(NULLIF(quote_timestamp, ''), fetched_at), 1, 10)
            WHERE trade_date IS NULL OR trade_date = ''
        """,
    )
    _run_once(
        conn,
        "20260714_quote_history_normalize_trade_date",
        """
            UPDATE quote_history
            SET trade_date = replace(
                substr(COALESCE(NULLIF(trade_date, ''), NULLIF(quote_timestamp, ''), fetched_at), 1, 10),
                '/',
                '-'
            )
            WHERE trade_date IS NULL OR trade_date = '' OR substr(trade_date, 1, 10) LIKE '____/__/__'
        """,
    )


def _apply_legacy_quote_history_deduplication(conn: sqlite3.Connection) -> None:
    _run_once(
        conn,
        "20260714_quote_history_daily_snapshot",
        """
            DELETE FROM quote_history
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY symbol, trade_date
                            ORDER BY replace(quote_timestamp, '/', '-') DESC, id DESC
                        ) AS duplicate_rank
                    FROM quote_history
                )
                WHERE duplicate_rank > 1
            )
        """,
    )


def _apply_monitor_event_migration(conn: sqlite3.Connection) -> None:
    if table_has_columns(conn, "monitor_event", "last_seen_at", "created_at", "repeat_count"):
        _run_once(
            conn,
            "20260612_monitor_event_repeat_fields",
            """
                UPDATE monitor_event
                SET
                    last_seen_at = COALESCE(last_seen_at, created_at),
                    repeat_count = COALESCE(NULLIF(repeat_count, 0), 1)
                WHERE last_seen_at IS NULL OR repeat_count IS NULL OR repeat_count <= 0
            """,
        )


def _ensure_quote_history_contract(conn: sqlite3.Connection) -> None:
    if _quote_history_requires_rebuild(conn):
        _rebuild_quote_history(conn)
    else:
        _delete_quote_history_duplicates(conn)
    for name in _QUOTE_HISTORY_MIGRATION_NAMES:
        conn.execute("INSERT OR IGNORE INTO schema_migration (name) VALUES (?)", (name,))


def _quote_history_requires_rebuild(conn: sqlite3.Connection) -> bool:
    columns = {_pragma_column_name(row): row for row in conn.execute("PRAGMA table_info(quote_history)")}
    trade_date = columns.get("trade_date")
    if trade_date is None:
        return False
    if not _pragma_column_not_null(trade_date) or _pragma_column_default(trade_date) is not None:
        return True
    table_sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'quote_history'").fetchone()
    table_sql = "" if table_sql_row is None else str(table_sql_row[0] or "")
    compact_sql = "".join(table_sql.lower().split())
    if "check(length(trim(trade_date))>0)" not in compact_sql:
        return True
    candidate = _quote_history_trade_date_expression()
    return (
        conn.execute(
            f"""
            SELECT 1
            FROM quote_history
            WHERE {candidate} IS NULL OR trade_date <> {candidate}
            LIMIT 1
            """
        ).fetchone()
        is not None
    )


def _rebuild_quote_history(conn: sqlite3.Connection) -> None:
    candidate = _quote_history_trade_date_expression()
    existing = {_pragma_column_name(row) for row in conn.execute("PRAGMA table_info(quote_history)")}
    columns_without_trade_date = tuple(column for column in _QUOTE_HISTORY_COLUMNS if column != "trade_date")
    insert_columns = ", ".join(_QUOTE_HISTORY_COLUMNS)
    select_columns = ", ".join("normalized_trade_date AS trade_date" if column == "trade_date" else column for column in _QUOTE_HISTORY_COLUMNS)
    source_columns = ", ".join(
        f"{_fallback_used_expression(existing)} AS fallback_used" if column == "fallback_used" else column for column in columns_without_trade_date
    )
    conn.execute(f"DROP TABLE IF EXISTS {_QUOTE_HISTORY_REBUILD_TABLE}")
    conn.execute(f"CREATE TABLE {_QUOTE_HISTORY_REBUILD_TABLE} ({QUOTE_HISTORY_COLUMN_DEFINITIONS})")
    conn.execute(
        f"""
        WITH normalized AS (
            SELECT
                {source_columns},
                {candidate} AS normalized_trade_date,
                ROW_NUMBER() OVER (
                    PARTITION BY symbol, {candidate}
                    ORDER BY replace(trim(COALESCE(quote_timestamp, '')), '/', '-') DESC, id DESC
                ) AS duplicate_rank
            FROM quote_history
        )
        INSERT INTO {_QUOTE_HISTORY_REBUILD_TABLE} ({insert_columns})
        SELECT {select_columns}
        FROM normalized
        WHERE normalized_trade_date IS NOT NULL AND duplicate_rank = 1
        """
    )
    conn.execute("DROP TABLE quote_history")
    conn.execute(f"ALTER TABLE {_QUOTE_HISTORY_REBUILD_TABLE} RENAME TO quote_history")


def _delete_quote_history_duplicates(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        DELETE FROM quote_history
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY symbol, trade_date
                        ORDER BY replace(trim(COALESCE(quote_timestamp, '')), '/', '-') DESC, id DESC
                    ) AS duplicate_rank
                FROM quote_history
            )
            WHERE duplicate_rank > 1
        )
        """
    )


def _quote_history_trade_date_expression() -> str:
    return "COALESCE({})".format(", ".join(_valid_date_expression(column) for column in ("trade_date", "quote_timestamp", "fetched_at")))


def _valid_date_expression(column: str) -> str:
    normalized = f"replace(substr(trim(COALESCE({column}, '')), 1, 10), '/', '-')"
    return f"CASE WHEN {normalized} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' " f"AND date({normalized}) = {normalized} THEN {normalized} END"


def _fallback_used_expression(existing: set[str]) -> str:
    if "fallback_used" not in existing:
        return "0"
    return "CASE WHEN COALESCE(fallback_used, 0) <> 0 THEN 1 ELSE 0 END"


def run_once(conn: sqlite3.Connection, name: str, sql: str) -> None:
    with migration_transaction(conn):
        _run_once(conn, name, sql)


def _run_once(conn: sqlite3.Connection, name: str, sql: str) -> None:
    ensure_migration_table(conn)
    claimed = conn.execute("INSERT OR IGNORE INTO schema_migration (name) VALUES (?)", (name,))
    if claimed.rowcount == 0:
        return
    conn.execute(sql)


def ensure_compat_indexes(conn: sqlite3.Connection) -> None:
    if table_has_columns(
        conn,
        "kline_daily",
        "symbol",
        "adjustment_mode",
        "date",
        "fetched_at",
    ):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_kline_symbol_date
                ON kline_daily(symbol, date DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_kline_daily_adjustment_fetch
                ON kline_daily(symbol, adjustment_mode, fetched_at, date)
            """
        )
    if table_has_columns(conn, "quote_history", "symbol", "trade_date", "fetched_at", "id"):
        for index in _OBSOLETE_QUOTE_HISTORY_INDEXES:
            conn.execute(f"DROP INDEX IF EXISTS {index}")
        _ensure_unique_index(
            conn,
            table="quote_history",
            index=QUOTE_HISTORY_UNIQUE_INDEX,
            columns=("symbol", "trade_date"),
        )
    if table_has_columns(conn, "monitor_event", "level", "category", "symbol", "message", "last_seen_at"):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_monitor_event_signature
                ON monitor_event(level, category, symbol, message)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_monitor_event_last_seen
                ON monitor_event(last_seen_at)
            """
        )


def audit_timestamp_migration_pending(conn: sqlite3.Connection) -> bool:
    """Return whether an existing database still needs audit-time normalization."""

    if table_exists(conn, "schema_migration"):
        try:
            completed = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM schema_migration WHERE name IN (?, ?)",
                    (
                        AUDIT_TIMESTAMP_UTC_MIGRATION,
                        ADVICE_REVIEW_AUDIT_UTC_SCHEMA_VERSION,
                    ),
                )
            }
        except sqlite3.DatabaseError:
            return True
        expected = {AUDIT_TIMESTAMP_UTC_MIGRATION}
        if table_exists(conn, "advice_review_plan") or table_exists(
            conn,
            "advice_review_result",
        ):
            expected.add(ADVICE_REVIEW_AUDIT_UTC_SCHEMA_VERSION)
        if expected.issubset(completed):
            return False
    return any(table_exists(conn, table) for table in AUDIT_TIMESTAMP_COLUMNS)


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA_MIGRATION_SQL)


def _ensure_wal_mode(conn: sqlite3.Connection) -> None:
    database = conn.execute("PRAGMA database_list").fetchone()
    if database is None or not str(database[2] or "").strip():
        return
    conn.execute(f"PRAGMA busy_timeout = {MIGRATION_BUSY_TIMEOUT_MS}")
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(JOURNAL_MODE_RETRY_COUNT):
        try:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is not None and str(mode[0]).lower() == "wal":
                return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            last_error = exc
        time.sleep(0.05 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise sqlite3.OperationalError("could not enable WAL journal mode")


@contextmanager
def migration_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        savepoint = f"compat_schema_{id(conn)}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except Exception:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            raise
        else:
            conn.execute(f"RELEASE {savepoint}")
        return

    conn.execute(f"PRAGMA busy_timeout = {MIGRATION_BUSY_TIMEOUT_MS}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _ensure_unique_index(
    conn: sqlite3.Connection,
    *,
    table: str,
    index: str,
    columns: tuple[str, ...],
) -> None:
    existing = next(
        (row for row in conn.execute(f"PRAGMA index_list({table})").fetchall() if _pragma_index_name(row) == index),
        None,
    )
    if existing is not None:
        existing_columns = tuple(_pragma_index_column_name(row) for row in conn.execute(f"PRAGMA index_info({index})"))
        if _pragma_index_unique(existing) and existing_columns == columns:
            return
        conn.execute(f"DROP INDEX {index}")
    column_sql = ", ".join(columns)
    conn.execute(f"CREATE UNIQUE INDEX {index} ON {table}({column_sql})")


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def table_has_columns(conn: sqlite3.Connection, table: str, *columns: str) -> bool:
    existing = {_pragma_column_name(row) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return bool(existing) and set(columns).issubset(existing)


def _pragma_column_name(row: sqlite3.Row | tuple) -> str:
    try:
        return str(row["name"])
    except (TypeError, IndexError):
        return str(row[1])


def _pragma_column_not_null(row: sqlite3.Row | tuple) -> bool:
    try:
        return bool(row["notnull"])
    except (TypeError, IndexError):
        return bool(row[3])


def _pragma_column_default(row: sqlite3.Row | tuple) -> object:
    try:
        return row["dflt_value"]
    except (TypeError, IndexError):
        return row[4]


def _pragma_column_primary_key_position(row: sqlite3.Row | tuple) -> int:
    try:
        return int(row["pk"])
    except (TypeError, IndexError):
        return int(row[5])


def _pragma_index_name(row: sqlite3.Row | tuple) -> str:
    try:
        return str(row["name"])
    except (TypeError, IndexError):
        return str(row[1])


def _pragma_index_unique(row: sqlite3.Row | tuple) -> bool:
    try:
        return bool(row["unique"])
    except (TypeError, IndexError):
        return bool(row[2])


def _pragma_index_column_name(row: sqlite3.Row | tuple) -> str:
    try:
        return str(row["name"])
    except (TypeError, IndexError):
        return str(row[2])


__all__ = [
    "AUDIT_TIMESTAMP_COLUMNS",
    "AUDIT_TIMESTAMP_UTC_MIGRATION",
    "COMPAT_COLUMNS",
    "JOURNAL_MODE_RETRY_COUNT",
    "KLINE_DAILY_CONTRACT_MIGRATION",
    "MIGRATION_BUSY_TIMEOUT_MS",
    "QUOTE_HISTORY_CONTRACT_MIGRATION",
    "QUOTE_HISTORY_UNIQUE_INDEX",
    "SCHEMA_MIGRATION_SQL",
    "SCHEMA_MIGRATION_APPLIED_AT_UTC_MIGRATION",
    "apply_compat_migrations",
    "apply_compat_schema",
    "ensure_column",
    "ensure_compat_indexes",
    "ensure_migration_table",
    "migration_transaction",
    "normalize_legacy_audit_timestamps",
    "run_once",
    "table_exists",
    "table_has_columns",
]
