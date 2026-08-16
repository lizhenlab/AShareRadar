"""Idempotent SQLite schema for immutable full-market strategy specifications."""

from __future__ import annotations

import sqlite3

from app.db.market_scan_integrity import verify_market_scan_snapshot


STRATEGY_LAB_SCHEMA_VERSION = "20260801_strategy_lab_v1"
STRATEGY_EXECUTION_SOURCE_DIGEST_SCHEMA_VERSION = (
    "20260813_strategy_execution_source_digest_v2"
)

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
    source_snapshot_digest TEXT NOT NULL CHECK (
        length(source_snapshot_digest) = 64
        AND source_snapshot_digest NOT GLOB '*[^0-9a-f]*'
    ),
    source_snapshot_seal_origin TEXT NOT NULL CHECK (
        source_snapshot_seal_origin IN ('publication', 'legacy_backfill')
    ),
    source_snapshot_verification_status TEXT NOT NULL DEFAULT 'verified' CHECK (
        source_snapshot_verification_status IN ('verified', 'legacy_unverified')
    ),
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

CREATE TRIGGER IF NOT EXISTS trg_strategy_execution_no_update
BEFORE UPDATE ON strategy_execution
BEGIN
    SELECT RAISE(ABORT, 'strategy_execution is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_strategy_execution_no_delete
BEFORE DELETE ON strategy_execution
BEGIN
    SELECT RAISE(ABORT, 'strategy_execution is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_strategy_execution_candidate_no_update
BEFORE UPDATE ON strategy_execution_candidate
BEGIN
    SELECT RAISE(ABORT, 'strategy_execution_candidate is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_strategy_execution_candidate_no_delete
BEFORE DELETE ON strategy_execution_candidate
BEGIN
    SELECT RAISE(ABORT, 'strategy_execution_candidate is append-only');
END;

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
    _apply_strategy_execution_source_digest_migration(conn)


def _apply_strategy_execution_source_digest_migration(
    conn: sqlite3.Connection,
) -> None:
    columns = _strategy_execution_columns(conn)
    if not columns:
        return
    if _source_digest_migration_is_current(conn, columns):
        _verify_strategy_execution_source_runs(conn)
        _require_safe_strategy_execution_sources(conn)
        _create_strategy_execution_integrity_triggers(conn)
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        _verify_strategy_execution_source_runs(conn)
        _drop_strategy_execution_source_write_guards(conn)
        _add_strategy_execution_source_columns(conn, columns)
        _backfill_strategy_execution_sources(conn)
        _require_safe_strategy_execution_sources(conn)
        _create_strategy_execution_integrity_triggers(conn)
        _record_strategy_execution_source_migration(conn)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _strategy_execution_columns(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(strategy_execution)").fetchall()
    }


def _source_digest_migration_is_current(
    conn: sqlite3.Connection,
    columns: set[str],
) -> bool:
    required = {
        "source_snapshot_digest",
        "source_snapshot_seal_origin",
        "source_snapshot_verification_status",
    }
    if not required.issubset(columns):
        return False
    row = conn.execute(
        "SELECT 1 FROM schema_migration WHERE name = ?",
        (STRATEGY_EXECUTION_SOURCE_DIGEST_SCHEMA_VERSION,),
    ).fetchone()
    return row is not None


def _verify_strategy_execution_source_runs(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT DISTINCT execution.market_scan_run_id
        FROM strategy_execution AS execution
        JOIN market_scan_run AS run ON run.id = execution.market_scan_run_id
        ORDER BY market_scan_run_id
        """
    ).fetchall()
    for row in rows:
        verify_market_scan_snapshot(conn, int(row[0]))


def _drop_strategy_execution_source_write_guards(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER IF EXISTS trg_strategy_execution_no_update")
    conn.execute(
        "DROP TRIGGER IF EXISTS trg_strategy_execution_source_digest_required"
    )


def _add_strategy_execution_source_columns(
    conn: sqlite3.Connection,
    columns: set[str],
) -> None:
    if "source_snapshot_digest" not in columns:
        conn.execute(
            """
            ALTER TABLE strategy_execution ADD COLUMN source_snapshot_digest TEXT
            CHECK (
                source_snapshot_digest IS NULL OR (
                    length(source_snapshot_digest) = 64
                    AND source_snapshot_digest NOT GLOB '*[^0-9a-f]*'
                )
            )
            """
        )
    if "source_snapshot_seal_origin" not in columns:
        conn.execute(
            """
            ALTER TABLE strategy_execution
            ADD COLUMN source_snapshot_seal_origin TEXT CHECK (
                source_snapshot_seal_origin IS NULL
                OR source_snapshot_seal_origin IN ('publication', 'legacy_backfill')
            )
            """
        )
    if "source_snapshot_verification_status" not in columns:
        conn.execute(
            """
            ALTER TABLE strategy_execution
            ADD COLUMN source_snapshot_verification_status TEXT NOT NULL
            DEFAULT 'legacy_unverified' CHECK (
                source_snapshot_verification_status
                    IN ('verified', 'legacy_unverified')
            )
            """
        )


def _backfill_strategy_execution_sources(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE strategy_execution
        SET source_snapshot_digest = (
            SELECT run.snapshot_digest FROM market_scan_run AS run
            WHERE run.id = strategy_execution.market_scan_run_id
        ),
            source_snapshot_seal_origin = (
                SELECT run.snapshot_seal_origin FROM market_scan_run AS run
                WHERE run.id = strategy_execution.market_scan_run_id
            ),
            source_snapshot_verification_status = 'verified'
        WHERE source_snapshot_digest IS NULL
          AND source_snapshot_seal_origin IS NULL
          AND EXISTS (
              SELECT 1 FROM market_scan_run AS run
              WHERE run.id = strategy_execution.market_scan_run_id
          )
        """
    )
    conn.execute(
        """
        UPDATE strategy_execution
        SET source_snapshot_verification_status = 'verified'
        WHERE EXISTS (
            SELECT 1 FROM market_scan_run AS run
            WHERE run.id = strategy_execution.market_scan_run_id
              AND run.snapshot_digest = strategy_execution.source_snapshot_digest
              AND run.snapshot_seal_origin = strategy_execution.source_snapshot_seal_origin
        )
        """
    )


def _require_safe_strategy_execution_sources(conn: sqlite3.Connection) -> None:
    unresolved = conn.execute(
        """
        SELECT COUNT(*)
        FROM strategy_execution AS execution
        LEFT JOIN market_scan_run AS run ON run.id = execution.market_scan_run_id
        WHERE (
            execution.source_snapshot_verification_status = 'verified'
            AND (
                run.id IS NULL
                OR execution.source_snapshot_digest IS NULL
                OR execution.source_snapshot_seal_origin IS NULL
                OR execution.source_snapshot_digest <> run.snapshot_digest
                OR execution.source_snapshot_seal_origin <> run.snapshot_seal_origin
            )
        ) OR (
            execution.source_snapshot_verification_status = 'legacy_unverified'
            AND (
                run.id IS NOT NULL
                OR execution.source_snapshot_digest IS NOT NULL
                OR execution.source_snapshot_seal_origin IS NOT NULL
            )
        )
        """
    ).fetchone()
    if unresolved is None or int(unresolved[0]):
        raise sqlite3.IntegrityError(
            "legacy strategy_execution source snapshot cannot be verified"
        )


def _record_strategy_execution_source_migration(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_migration (name) VALUES (?)",
        (STRATEGY_EXECUTION_SOURCE_DIGEST_SCHEMA_VERSION,),
    )


def _create_strategy_execution_integrity_triggers(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_strategy_execution_no_update
        BEFORE UPDATE ON strategy_execution
        BEGIN
            SELECT RAISE(ABORT, 'strategy_execution is append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_strategy_execution_source_digest_required
        BEFORE INSERT ON strategy_execution
        WHEN NEW.source_snapshot_digest IS NULL
          OR length(NEW.source_snapshot_digest) <> 64
          OR NEW.source_snapshot_digest GLOB '*[^0-9a-f]*'
          OR NEW.source_snapshot_seal_origin NOT IN ('publication', 'legacy_backfill')
          OR NEW.source_snapshot_verification_status <> 'verified'
        BEGIN
            SELECT RAISE(ABORT, 'strategy_execution source snapshot digest is required');
        END
        """
    )


__all__ = [
    "STRATEGY_LAB_SCHEMA_SQL",
    "STRATEGY_LAB_SCHEMA_VERSION",
    "STRATEGY_EXECUTION_SOURCE_DIGEST_SCHEMA_VERSION",
    "apply_strategy_lab_schema",
]
