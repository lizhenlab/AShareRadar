from __future__ import annotations

import sqlite3


SCHEMA_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

COMPAT_COLUMNS = {
    "advice_history": {
        "updated_at": "TEXT",
        "repeat_count": "INTEGER NOT NULL DEFAULT 1",
    },
    "quote_history": {
        "pe": "REAL",
        "pb": "REAL",
        "market_cap": "REAL",
        "trade_date": "TEXT",
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
    "stock_concept": {
        "match_reason": "TEXT NOT NULL DEFAULT '概念成分匹配'",
    },
}


def apply_compat_schema(conn: sqlite3.Connection) -> None:
    ensure_migration_table(conn)
    for table, columns in COMPAT_COLUMNS.items():
        if not table_exists(conn, table):
            continue
        for column, definition in columns.items():
            ensure_column(conn, table, column, definition)
    apply_compat_migrations(conn)
    ensure_compat_indexes(conn)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        return
    if any(_pragma_column_name(row) == column for row in rows):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def apply_compat_migrations(conn: sqlite3.Connection) -> None:
    ensure_migration_table(conn)
    if table_has_columns(conn, "quote_history", "trade_date", "quote_timestamp", "fetched_at"):
        run_once(
            conn,
            "20260612_quote_history_trade_date",
            """
                UPDATE quote_history
                SET trade_date = substr(COALESCE(NULLIF(quote_timestamp, ''), fetched_at), 1, 10)
                WHERE trade_date IS NULL OR trade_date = ''
            """,
        )
    if table_has_columns(conn, "monitor_event", "last_seen_at", "created_at", "repeat_count"):
        run_once(
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


def run_once(conn: sqlite3.Connection, name: str, sql: str) -> None:
    ensure_migration_table(conn)
    exists = conn.execute("SELECT name FROM schema_migration WHERE name = ?", (name,)).fetchone()
    if exists:
        return
    conn.execute(sql)
    conn.execute("INSERT INTO schema_migration (name) VALUES (?)", (name,))


def ensure_compat_indexes(conn: sqlite3.Connection) -> None:
    if table_has_columns(conn, "quote_history", "symbol", "trade_date", "fetched_at", "id"):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_quote_history_symbol_trade_date_time
                ON quote_history(symbol, trade_date, fetched_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_quote_history_symbol_trade_latest
                ON quote_history(symbol, trade_date DESC, fetched_at DESC, id DESC)
            """
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


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA_MIGRATION_SQL)


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


__all__ = [
    "COMPAT_COLUMNS",
    "SCHEMA_MIGRATION_SQL",
    "apply_compat_migrations",
    "apply_compat_schema",
    "ensure_column",
    "ensure_compat_indexes",
    "ensure_migration_table",
    "run_once",
    "table_exists",
    "table_has_columns",
]
