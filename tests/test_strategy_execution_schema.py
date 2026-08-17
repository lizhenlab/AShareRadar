from __future__ import annotations

import sqlite3

import pytest

from app.db import strategy_lab_schema


def test_source_digest_migration_fault_rolls_back_and_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE schema_migration (name TEXT PRIMARY KEY);
        CREATE TABLE market_scan_run (
            id INTEGER PRIMARY KEY,
            snapshot_digest TEXT,
            snapshot_seal_origin TEXT
        );
        CREATE TABLE strategy_execution (
            id INTEGER PRIMARY KEY,
            market_scan_run_id INTEGER NOT NULL
        );
        CREATE TRIGGER trg_strategy_execution_no_update
        BEFORE UPDATE ON strategy_execution
        BEGIN
            SELECT RAISE(ABORT, 'strategy_execution is append-only');
        END;
        """
    )

    with monkeypatch.context() as patcher:
        patcher.setattr(
            strategy_lab_schema,
            "_backfill_strategy_execution_sources",
            lambda _conn: (_ for _ in ()).throw(RuntimeError("injected fault")),
        )
        with pytest.raises(RuntimeError, match="injected fault"):
            strategy_lab_schema._apply_strategy_execution_source_digest_migration(conn)

    assert _column_names(conn) == {"id", "market_scan_run_id"}
    assert _trigger_names(conn) == {"trg_strategy_execution_no_update"}
    assert not _source_migration_recorded(conn)

    strategy_lab_schema._apply_strategy_execution_source_digest_migration(conn)

    assert {
        "source_snapshot_digest",
        "source_snapshot_seal_origin",
    }.issubset(_column_names(conn))
    assert {
        "trg_strategy_execution_no_update",
        "trg_strategy_execution_source_digest_required",
    }.issubset(_trigger_names(conn))
    assert _source_migration_recorded(conn)


def test_current_source_digest_marker_rejects_binding_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        f"""
        CREATE TABLE schema_migration (name TEXT PRIMARY KEY);
        CREATE TABLE market_scan_run (
            id INTEGER PRIMARY KEY,
            snapshot_digest TEXT,
            snapshot_seal_origin TEXT
        );
        CREATE TABLE strategy_execution (
            id INTEGER PRIMARY KEY,
            market_scan_run_id INTEGER NOT NULL,
            source_snapshot_digest TEXT,
            source_snapshot_seal_origin TEXT
        );
        INSERT INTO market_scan_run VALUES (1, '{'a' * 64}', 'legacy_backfill');
        INSERT INTO strategy_execution VALUES (1, 1, '{'b' * 64}', 'publication');
        INSERT INTO schema_migration VALUES (
            '{strategy_lab_schema.STRATEGY_EXECUTION_SOURCE_DIGEST_SCHEMA_VERSION}'
        );
        """
    )
    monkeypatch.setattr(
        strategy_lab_schema,
        "_verify_strategy_execution_source_runs",
        lambda _conn: None,
    )

    with pytest.raises(sqlite3.IntegrityError, match="cannot be verified"):
        strategy_lab_schema._apply_strategy_execution_source_digest_migration(conn)


def test_orphan_legacy_execution_is_preserved_as_unverified_audit_only() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE schema_migration (name TEXT PRIMARY KEY);
        CREATE TABLE market_scan_run (
            id INTEGER PRIMARY KEY,
            snapshot_digest TEXT,
            snapshot_seal_origin TEXT
        );
        CREATE TABLE strategy_execution (
            id INTEGER PRIMARY KEY,
            market_scan_run_id INTEGER NOT NULL
        );
        INSERT INTO strategy_execution VALUES (1, 16);
        CREATE TRIGGER trg_strategy_execution_no_update
        BEFORE UPDATE ON strategy_execution
        BEGIN
            SELECT RAISE(ABORT, 'strategy_execution is append-only');
        END;
        """
    )

    strategy_lab_schema._apply_strategy_execution_source_digest_migration(conn)
    strategy_lab_schema._apply_strategy_execution_source_digest_migration(conn)

    row = conn.execute(
        """
        SELECT market_scan_run_id, source_snapshot_digest,
               source_snapshot_seal_origin,
               source_snapshot_verification_status
        FROM strategy_execution WHERE id = 1
        """
    ).fetchone()
    assert row == (16, None, None, "legacy_unverified")
    assert _source_migration_recorded(conn)
    with pytest.raises(sqlite3.IntegrityError, match="source snapshot digest"):
        conn.execute(
            "INSERT INTO strategy_execution (id, market_scan_run_id) VALUES (2, 17)"
        )


def _column_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(strategy_execution)")}


def _trigger_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }


def _source_migration_recorded(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM schema_migration WHERE name = ?",
        (strategy_lab_schema.STRATEGY_EXECUTION_SOURCE_DIGEST_SCHEMA_VERSION,),
    ).fetchone()
    return row is not None
