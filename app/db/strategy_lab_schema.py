"""Idempotent SQLite schema for immutable full-market strategy specifications."""

from __future__ import annotations

import sqlite3


STRATEGY_LAB_SCHEMA_VERSION = "20260801_strategy_lab_v1"

STRATEGY_LAB_SCHEMA_SQL = f"""
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS strategy_spec (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    current_revision INTEGER NOT NULL DEFAULT 1 CHECK (current_revision >= 1),
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);

CREATE TABLE IF NOT EXISTS strategy_spec_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL CHECK (strategy_id > 0),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 80),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 1000),
    spec_json TEXT NOT NULL CHECK (json_valid(spec_json)),
    fingerprint TEXT NOT NULL CHECK (
        length(fingerprint) = 64
        AND fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (strategy_id) REFERENCES strategy_spec(id) ON DELETE CASCADE,
    UNIQUE (strategy_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_strategy_spec_updated
    ON strategy_spec(archived ASC, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_spec_version_strategy
    ON strategy_spec_version(strategy_id, revision DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_spec_version_fingerprint
    ON strategy_spec_version(fingerprint, strategy_id, revision);

CREATE TABLE IF NOT EXISTS strategy_execution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL CHECK (strategy_id > 0),
    strategy_revision INTEGER NOT NULL CHECK (strategy_revision > 0),
    strategy_fingerprint TEXT NOT NULL CHECK (length(strategy_fingerprint) = 64),
    execution_fingerprint TEXT NOT NULL CHECK (length(execution_fingerprint) = 64),
    kind TEXT NOT NULL CHECK (kind IN ('latest_scan', 'historical_replay')),
    market_scan_run_id INTEGER NOT NULL CHECK (market_scan_run_id > 0),
    rule_version TEXT NOT NULL CHECK (length(trim(rule_version)) > 0),
    data_as_of TEXT NOT NULL CHECK (length(trim(data_as_of)) > 0),
    data_date TEXT NOT NULL CHECK (length(data_date) = 10),
    cost_rule_fingerprint TEXT NOT NULL CHECK (length(cost_rule_fingerprint) = 64),
    status TEXT NOT NULL CHECK (status IN ('ready', 'no_trade', 'blocked')),
    summary_json TEXT NOT NULL CHECK (json_valid(summary_json)),
    result_digest TEXT NOT NULL CHECK (length(result_digest) = 64),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (strategy_id, strategy_revision)
        REFERENCES strategy_spec_version(strategy_id, revision) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS strategy_execution_candidate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL CHECK (execution_id > 0),
    symbol TEXT NOT NULL CHECK (length(trim(symbol)) > 0),
    original_rank INTEGER,
    utility_rank INTEGER,
    status TEXT NOT NULL CHECK (
        status IN ('selected', 'rejected', 'constraint_adjusted', 'unfilled')
    ),
    target_weight REAL NOT NULL DEFAULT 0 CHECK (target_weight >= 0 AND target_weight <= 1),
    pareto_front INTEGER NOT NULL DEFAULT 0 CHECK (pareto_front IN (0, 1)),
    candidate_json TEXT NOT NULL CHECK (json_valid(candidate_json)),
    FOREIGN KEY (execution_id) REFERENCES strategy_execution(id) ON DELETE CASCADE,
    UNIQUE (execution_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_strategy_execution_strategy_time
    ON strategy_execution(strategy_id, strategy_revision, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_execution_scan
    ON strategy_execution(market_scan_run_id, strategy_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_execution_candidate_page
    ON strategy_execution_candidate(execution_id, utility_rank ASC, original_rank ASC, symbol ASC);
CREATE INDEX IF NOT EXISTS idx_strategy_execution_candidate_status
    ON strategy_execution_candidate(execution_id, status, utility_rank ASC);

CREATE TABLE IF NOT EXISTS strategy_evidence_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL CHECK (strategy_id > 0),
    strategy_revision INTEGER NOT NULL CHECK (strategy_revision > 0),
    strategy_fingerprint TEXT NOT NULL CHECK (length(strategy_fingerprint) = 64),
    mode TEXT NOT NULL CHECK (mode IN ('official', 'intraday')),
    status TEXT NOT NULL CHECK (
        status IN ('insufficient_data', 'blocked', 'eligible_for_manual_review')
    ),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    evidence_digest TEXT NOT NULL CHECK (length(evidence_digest) = 64),
    generated_at TEXT NOT NULL CHECK (length(trim(generated_at)) > 0),
    FOREIGN KEY (strategy_id, strategy_revision)
        REFERENCES strategy_spec_version(strategy_id, revision) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_strategy_evidence_latest
    ON strategy_evidence_snapshot(strategy_id, strategy_revision, mode, generated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS strategy_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL CHECK (strategy_id > 0),
    strategy_revision INTEGER NOT NULL CHECK (strategy_revision > 0),
    strategy_fingerprint TEXT NOT NULL CHECK (length(strategy_fingerprint) = 64),
    cadence TEXT NOT NULL CHECK (cadence IN ('daily_after_close', 'trading_day_intraday')),
    mode TEXT NOT NULL CHECK (mode IN ('official', 'intraday')),
    notional_cash_cny REAL NOT NULL CHECK (notional_cash_cny BETWEEN 10000 AND 1000000000),
    alert_conditions_json TEXT NOT NULL CHECK (json_valid(alert_conditions_json)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_execution_id INTEGER,
    last_market_scan_run_id INTEGER,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    FOREIGN KEY (strategy_id, strategy_revision)
        REFERENCES strategy_spec_version(strategy_id, revision) ON DELETE RESTRICT,
    FOREIGN KEY (last_execution_id) REFERENCES strategy_execution(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS strategy_alert_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL CHECK (schedule_id > 0),
    strategy_id INTEGER NOT NULL CHECK (strategy_id > 0),
    strategy_revision INTEGER NOT NULL CHECK (strategy_revision > 0),
    strategy_fingerprint TEXT NOT NULL CHECK (length(strategy_fingerprint) = 64),
    execution_id INTEGER NOT NULL CHECK (execution_id > 0),
    execution_fingerprint TEXT NOT NULL CHECK (length(execution_fingerprint) = 64),
    data_as_of TEXT NOT NULL CHECK (length(trim(data_as_of)) > 0),
    event_type TEXT NOT NULL CHECK (
        event_type IN ('new_entry', 'removed', 'utility_cross', 'data_stale', 'evidence_invalid')
    ),
    symbol TEXT,
    message TEXT NOT NULL CHECK (length(trim(message)) > 0),
    trigger_json TEXT NOT NULL CHECK (json_valid(trigger_json)),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (schedule_id) REFERENCES strategy_schedule(id) ON DELETE CASCADE,
    FOREIGN KEY (execution_id) REFERENCES strategy_execution(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS strategy_schedule_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL CHECK (schedule_id > 0),
    market_scan_run_id INTEGER NOT NULL CHECK (market_scan_run_id > 0),
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    execution_id INTEGER,
    error TEXT,
    started_at TEXT NOT NULL CHECK (length(trim(started_at)) > 0),
    finished_at TEXT,
    FOREIGN KEY (schedule_id) REFERENCES strategy_schedule(id) ON DELETE CASCADE,
    FOREIGN KEY (execution_id) REFERENCES strategy_execution(id) ON DELETE SET NULL,
    UNIQUE (schedule_id, market_scan_run_id)
);

CREATE TABLE IF NOT EXISTS strategy_simulation_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL CHECK (execution_id > 0),
    strategy_id INTEGER NOT NULL CHECK (strategy_id > 0),
    strategy_revision INTEGER NOT NULL CHECK (strategy_revision > 0),
    strategy_fingerprint TEXT NOT NULL CHECK (length(strategy_fingerprint) = 64),
    execution_fingerprint TEXT NOT NULL CHECK (length(execution_fingerprint) = 64),
    rule_version TEXT NOT NULL CHECK (length(trim(rule_version)) > 0),
    data_as_of TEXT NOT NULL CHECK (length(trim(data_as_of)) > 0),
    cost_rule_fingerprint TEXT NOT NULL CHECK (length(cost_rule_fingerprint) = 64),
    status TEXT NOT NULL CHECK (status IN ('draft', 'no_trade')),
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json)),
    plan_digest TEXT NOT NULL CHECK (length(plan_digest) = 64),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (execution_id) REFERENCES strategy_execution(id) ON DELETE RESTRICT,
    UNIQUE (execution_id)
);

CREATE INDEX IF NOT EXISTS idx_strategy_schedule_enabled
    ON strategy_schedule(enabled, mode, updated_at, id);
CREATE INDEX IF NOT EXISTS idx_strategy_alert_event_schedule
    ON strategy_alert_event(schedule_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_schedule_run_status
    ON strategy_schedule_run(status, started_at, id);
CREATE INDEX IF NOT EXISTS idx_strategy_simulation_plan_strategy
    ON strategy_simulation_plan(strategy_id, strategy_revision, created_at DESC, id DESC);

INSERT OR IGNORE INTO schema_migration (name) VALUES ('{STRATEGY_LAB_SCHEMA_VERSION}');

COMMIT;
"""


def apply_strategy_lab_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(STRATEGY_LAB_SCHEMA_SQL)


__all__ = [
    "STRATEGY_LAB_SCHEMA_SQL",
    "STRATEGY_LAB_SCHEMA_VERSION",
    "apply_strategy_lab_schema",
]
