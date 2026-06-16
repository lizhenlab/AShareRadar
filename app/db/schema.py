from __future__ import annotations

import sqlite3


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS provider_status (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    healthy INTEGER NOT NULL,
    last_success TEXT,
    last_error TEXT,
    latency_ms REAL,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_capability_status (
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    healthy INTEGER NOT NULL,
    last_success TEXT,
    last_error TEXT,
    latency_ms REAL,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (name, kind)
);

CREATE TABLE IF NOT EXISTS quote_snapshot (
    symbol TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    name TEXT NOT NULL,
    price REAL NOT NULL,
    prev_close REAL NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    volume REAL NOT NULL,
    amount REAL NOT NULL,
    change REAL NOT NULL,
    change_pct REAL NOT NULL,
    turnover_rate REAL,
    pe REAL,
    pb REAL,
    market_cap REAL,
    quote_timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quote_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    name TEXT NOT NULL,
    price REAL NOT NULL,
    change_pct REAL NOT NULL,
    pe REAL,
    pb REAL,
    market_cap REAL,
    source TEXT NOT NULL,
    quote_timestamp TEXT NOT NULL,
    trade_date TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kline_daily (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL NOT NULL,
    close REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    volume REAL NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS kline_minute (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open REAL NOT NULL,
    close REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    volume REAL NOT NULL,
    amount REAL,
    turnover_rate REAL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, timestamp)
);

CREATE TABLE IF NOT EXISTS cache_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    message TEXT
);

CREATE TABLE IF NOT EXISTS monitor_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    symbol TEXT,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    repeat_count INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    name TEXT NOT NULL,
    note TEXT,
    group_name TEXT NOT NULL DEFAULT '默认',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS advice_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    name TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    trend_score INTEGER NOT NULL,
    trend_label TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    price REAL NOT NULL,
    change_pct REAL NOT NULL,
    support REAL NOT NULL,
    resistance REAL NOT NULL,
    data_quality_score INTEGER NOT NULL,
    data_quality_level TEXT NOT NULL,
    reason TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    repeat_count INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS alert_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    name TEXT NOT NULL,
    condition_type TEXT NOT NULL,
    threshold REAL NOT NULL,
    note TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    last_triggered_at TEXT,
    last_state TEXT NOT NULL DEFAULT '等待',
    trigger_count INTEGER NOT NULL DEFAULT 0,
    cooldown_seconds INTEGER NOT NULL DEFAULT 300,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    name TEXT NOT NULL,
    condition_type TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT '触发',
    message TEXT NOT NULL,
    price REAL NOT NULL,
    change_pct REAL NOT NULL,
    threshold REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_note (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    name TEXT NOT NULL,
    note_type TEXT NOT NULL,
    content TEXT NOT NULL,
    price REAL,
    trade_date TEXT,
    color TEXT,
    visible INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_master (
    symbol TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    name TEXT NOT NULL,
    industry TEXT,
    list_date TEXT,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plate_rank (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rank INTEGER NOT NULL,
    name TEXT NOT NULL,
    change_pct REAL NOT NULL,
    amount REAL,
    turnover_rate REAL,
    leading_stock TEXT,
    leading_stock_change_pct REAL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_concept (
    symbol TEXT NOT NULL,
    rank INTEGER NOT NULL,
    name TEXT NOT NULL,
    change_pct REAL NOT NULL DEFAULT 0,
    amount REAL,
    turnover_rate REAL,
    leading_stock TEXT,
    leading_stock_change_pct REAL,
    match_reason TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, name)
);

CREATE TABLE IF NOT EXISTS schema_migration (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_quote_history_symbol_time
    ON quote_history(symbol, fetched_at);
CREATE INDEX IF NOT EXISTS idx_quote_history_symbol_trade_latest
    ON quote_history(symbol, trade_date DESC, fetched_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_kline_symbol_date
    ON kline_daily(symbol, date);
CREATE INDEX IF NOT EXISTS idx_kline_minute_symbol_time
    ON kline_minute(symbol, interval, timestamp);
CREATE INDEX IF NOT EXISTS idx_stock_master_code
    ON stock_master(code);
CREATE INDEX IF NOT EXISTS idx_plate_rank_updated
    ON plate_rank(updated_at);
CREATE INDEX IF NOT EXISTS idx_stock_concept_symbol_updated
    ON stock_concept(symbol, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_run_started
    ON task_run(started_at);
CREATE INDEX IF NOT EXISTS idx_monitor_event_created
    ON monitor_event(created_at);
CREATE INDEX IF NOT EXISTS idx_watchlist_updated
    ON watchlist(updated_at);
CREATE INDEX IF NOT EXISTS idx_advice_history_symbol_created
    ON advice_history(symbol, created_at);
CREATE INDEX IF NOT EXISTS idx_alert_rule_symbol_enabled
    ON alert_rule(symbol, enabled);
CREATE INDEX IF NOT EXISTS idx_alert_event_symbol_created
    ON alert_event(symbol, created_at);
CREATE INDEX IF NOT EXISTS idx_alert_event_rule_created
    ON alert_event(rule_id, created_at);
CREATE INDEX IF NOT EXISTS idx_stock_note_symbol_created
    ON stock_note(symbol, created_at);
CREATE INDEX IF NOT EXISTS idx_provider_capability_status_name_kind
    ON provider_capability_status(name, kind);
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


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    for table, columns in COMPAT_COLUMNS.items():
        for column, definition in columns.items():
            ensure_column(conn, table, column, definition)
    apply_compat_migrations(conn)
    ensure_compat_indexes(conn)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if any(row["name"] == column for row in rows):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def apply_compat_migrations(conn: sqlite3.Connection) -> None:
    run_once(
        conn,
        "20260612_quote_history_trade_date",
        """
            UPDATE quote_history
            SET trade_date = substr(COALESCE(NULLIF(quote_timestamp, ''), fetched_at), 1, 10)
            WHERE trade_date IS NULL OR trade_date = ''
        """,
    )
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
    exists = conn.execute("SELECT name FROM schema_migration WHERE name = ?", (name,)).fetchone()
    if exists:
        return
    conn.execute(sql)
    conn.execute("INSERT INTO schema_migration (name) VALUES (?)", (name,))


def ensure_compat_indexes(conn: sqlite3.Connection) -> None:
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
