from __future__ import annotations


QUOTE_HISTORY_COLUMN_DEFINITIONS = """
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
    trade_date TEXT NOT NULL CHECK (length(trim(trade_date)) > 0),
    fetched_at TEXT NOT NULL
"""


KLINE_DAILY_COLUMN_DEFINITIONS = """
    symbol TEXT NOT NULL,
    adjustment_mode TEXT NOT NULL DEFAULT 'unknown'
        CHECK (adjustment_mode IN ('qfq', 'hfq', 'none', 'unknown')),
    date TEXT NOT NULL,
    open REAL NOT NULL,
    close REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    volume REAL NOT NULL,
    as_of TEXT,
    data_version TEXT NOT NULL DEFAULT 'legacy' CHECK (length(trim(data_version)) > 0),
    contract_version TEXT NOT NULL DEFAULT 'legacy' CHECK (length(trim(contract_version)) > 0),
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, adjustment_mode, date)
"""


SCHEMA_SQL = f"""
PRAGMA busy_timeout = 15000;
BEGIN IMMEDIATE;

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
{QUOTE_HISTORY_COLUMN_DEFINITIONS}
);

CREATE TABLE IF NOT EXISTS kline_daily (
{KLINE_DAILY_COLUMN_DEFINITIONS}
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
    research_status TEXT NOT NULL DEFAULT 'watching'
        CHECK (research_status IN ('to_research', 'watching', 'holding_research', 'excluded')),
    priority TEXT NOT NULL DEFAULT 'medium'
        CHECK (priority IN ('high', 'medium', 'low')),
    next_review_date TEXT,
    last_viewed_at TEXT,
    unread_change_count INTEGER NOT NULL DEFAULT 0 CHECK (unread_change_count >= 0),
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
    repeat_count INTEGER NOT NULL DEFAULT 1,
    snapshot_contract_version TEXT NOT NULL DEFAULT 'legacy',
    conclusion_basis TEXT NOT NULL DEFAULT 'legacy_unknown',
    rule_version TEXT NOT NULL DEFAULT 'unknown',
    model_version TEXT NOT NULL DEFAULT 'unknown',
    market_time TEXT,
    data_quality_source TEXT,
    kline_adjustment_mode TEXT NOT NULL DEFAULT 'unknown',
    kline_anchor_date TEXT,
    kline_anchor_close REAL,
    kline_data_version TEXT NOT NULL DEFAULT 'unknown',
    kline_contract_version TEXT NOT NULL DEFAULT 'unknown'
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

-- Indexes that use compatibility-added columns are created after migrations.
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
COMMIT;
"""


__all__ = [
    "KLINE_DAILY_COLUMN_DEFINITIONS",
    "QUOTE_HISTORY_COLUMN_DEFINITIONS",
    "SCHEMA_SQL",
]
