from __future__ import annotations


SCHEMA_MIGRATION_APPLIED_AT_DEFAULT_SQL = "(strftime('%Y-%m-%dT%H:%M:%f000Z', 'now'))"


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
    fallback_used INTEGER NOT NULL DEFAULT 0 CHECK (fallback_used IN (0, 1)),
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
    fallback_used INTEGER NOT NULL DEFAULT 0 CHECK (fallback_used IN (0, 1)),
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
    fallback_used INTEGER NOT NULL DEFAULT 0 CHECK (fallback_used IN (0, 1)),
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
    fallback_used INTEGER NOT NULL DEFAULT 0 CHECK (fallback_used IN (0, 1)),
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

CREATE TABLE IF NOT EXISTS reliability_bucket (
    bucket_start_utc TEXT NOT NULL,
    metric TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    capability TEXT NOT NULL DEFAULT '',
    samples INTEGER NOT NULL DEFAULT 0 CHECK (samples >= 0),
    good INTEGER NOT NULL DEFAULT 0 CHECK (good >= 0),
    degraded INTEGER NOT NULL DEFAULT 0 CHECK (degraded >= 0),
    failures INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
    fallback INTEGER NOT NULL DEFAULT 0 CHECK (fallback >= 0),
    duration_total_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_total_ms >= 0),
    duration_max_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_max_ms >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (bucket_start_utc, metric, subject, capability)
);

CREATE TABLE IF NOT EXISTS market_scan_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_run_id INTEGER,
    retry_of_run_id INTEGER,
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'running', 'cancelling', 'success', 'degraded', 'failed', 'cancelled', 'interrupted')),
    trigger TEXT NOT NULL
        CHECK (trigger IN ('manual', 'scheduled', 'retry')),
    mode TEXT NOT NULL DEFAULT 'official'
        CHECK (mode IN ('official', 'intraday', 'preopen')),
    rule_version TEXT NOT NULL,
    as_of TEXT NOT NULL,
    data_date TEXT NOT NULL,
    quote_date TEXT,
    scope TEXT NOT NULL,
    stock_pool_source TEXT,
    total_count INTEGER NOT NULL DEFAULT 0 CHECK (total_count >= 0),
    excluded_count INTEGER NOT NULL DEFAULT 0 CHECK (excluded_count >= 0),
    processed_count INTEGER NOT NULL DEFAULT 0 CHECK (processed_count >= 0),
    success_count INTEGER NOT NULL DEFAULT 0 CHECK (success_count >= 0),
    missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0),
    skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    quote_capture_started_at TEXT,
    quote_capture_finished_at TEXT,
    quote_capture_duration_ms INTEGER
        CHECK (quote_capture_duration_ms IS NULL OR quote_capture_duration_ms >= 0),
    quote_capture_count INTEGER NOT NULL DEFAULT 0
        CHECK (quote_capture_count >= 0),
    current_stage TEXT
        CHECK (current_stage IS NULL OR current_stage IN ('stock_pool', 'bulk_quotes', 'klines', 'scoring', 'persistence', 'publication')),
    stage_started_at TEXT,
    stage_metrics_json TEXT NOT NULL DEFAULT '{{}}',
    market_progress_json TEXT NOT NULL DEFAULT '[]',
    message TEXT,
    last_error TEXT,
    publication_diagnostics_json TEXT,
    cancel_requested_at TEXT,
    FOREIGN KEY (task_run_id) REFERENCES task_run(id) ON DELETE SET NULL,
    FOREIGN KEY (retry_of_run_id) REFERENCES market_scan_run(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS market_scan_result (
    run_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    code TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('SH', 'SZ', 'BJ')),
    name TEXT NOT NULL,
    industry TEXT,
    list_date TEXT,
    is_st INTEGER NOT NULL DEFAULT 0 CHECK (is_st IN (0, 1)),
    is_new INTEGER NOT NULL DEFAULT 0 CHECK (is_new IN (0, 1)),
    metadata_source TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'success', 'missing', 'skipped')),
    rank INTEGER CHECK (rank IS NULL OR rank > 0),
    score INTEGER CHECK (score IS NULL OR score BETWEEN 0 AND 100),
    raw_score REAL CHECK (raw_score IS NULL OR raw_score BETWEEN 0 AND 100),
    trend_score INTEGER CHECK (trend_score IS NULL OR trend_score BETWEEN 0 AND 100),
    leader_score INTEGER CHECK (leader_score IS NULL OR leader_score BETWEEN 0 AND 100),
    data_quality_score INTEGER CHECK (data_quality_score IS NULL OR data_quality_score BETWEEN 0 AND 100),
    price REAL,
    change_pct REAL,
    turnover_rate REAL,
    volume_ratio REAL,
    amount REAL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    metrics_json TEXT NOT NULL DEFAULT '{{}}',
    reason TEXT,
    error TEXT,
    data_date TEXT,
    quote_timestamp TEXT,
    quote_observed_at TEXT,
    quote_source TEXT,
    kline_source TEXT,
    adjustment_mode TEXT,
    quote_fallback_used INTEGER NOT NULL DEFAULT 0
        CHECK (quote_fallback_used IN (0, 1)),
    kline_fallback_used INTEGER NOT NULL DEFAULT 0
        CHECK (kline_fallback_used IN (0, 1)),
    metadata_degraded INTEGER NOT NULL DEFAULT 0
        CHECK (metadata_degraded IN (0, 1)),
    degradation_reasons_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, symbol),
    FOREIGN KEY (run_id) REFERENCES market_scan_run(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS market_scan_probability_capture_outbox (
    run_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'succeeded', 'skipped')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_attempt_at TEXT,
    completed_at TEXT,
    archive_digest TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES market_scan_run(id) ON DELETE CASCADE,
    CHECK (
        (status = 'processing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status <> 'processing' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    )
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
    applied_at TEXT NOT NULL DEFAULT {SCHEMA_MIGRATION_APPLIED_AT_DEFAULT_SQL}
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
CREATE INDEX IF NOT EXISTS idx_reliability_bucket_metric_time
    ON reliability_bucket(metric, bucket_start_utc DESC);
CREATE INDEX IF NOT EXISTS idx_market_scan_run_created
    ON market_scan_run(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_market_scan_run_status
    ON market_scan_run(status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_market_scan_single_active
    ON market_scan_run((1))
    WHERE status IN ('queued', 'running', 'cancelling');
CREATE INDEX IF NOT EXISTS idx_market_scan_result_rank
    ON market_scan_result(run_id, status, rank, symbol);
CREATE INDEX IF NOT EXISTS idx_market_scan_result_filters
    ON market_scan_result(run_id, market, industry, is_st, status);
CREATE INDEX IF NOT EXISTS idx_market_scan_probability_capture_due
    ON market_scan_probability_capture_outbox(status, next_attempt_at, run_id);
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
    "SCHEMA_MIGRATION_APPLIED_AT_DEFAULT_SQL",
    "SCHEMA_SQL",
]
