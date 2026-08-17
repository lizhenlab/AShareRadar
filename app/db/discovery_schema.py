from __future__ import annotations

import sqlite3


DISCOVERY_SCHEMA_SQL = """
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS discovery_preset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE
        CHECK (length(trim(name)) BETWEEN 1 AND 80),
    schema_version INTEGER NOT NULL DEFAULT 2 CHECK (schema_version IN (1, 2)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    criteria_json TEXT NOT NULL CHECK (json_valid(criteria_json)),
    sort_json TEXT NOT NULL CHECK (json_valid(sort_json)),
    column_view TEXT NOT NULL DEFAULT 'overview'
        CHECK (column_view IN ('overview', 'trend', 'liquidity', 'risk', 'research')),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_research_queue_source (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    source_run_id INTEGER NOT NULL CHECK (source_run_id > 0),
    source_preset_id INTEGER NOT NULL CHECK (source_preset_id > 0),
    source_preset_revision INTEGER NOT NULL CHECK (source_preset_revision > 0),
    source_preset_name TEXT NOT NULL
        CHECK (length(trim(source_preset_name)) BETWEEN 1 AND 80),
    preset_schema_version INTEGER NOT NULL CHECK (preset_schema_version IN (1, 2)),
    preset_snapshot_json TEXT NOT NULL CHECK (json_valid(preset_snapshot_json)),
    enqueued_at TEXT NOT NULL CHECK (length(trim(enqueued_at)) > 0),
    FOREIGN KEY (symbol) REFERENCES watchlist(symbol) ON DELETE CASCADE,
    UNIQUE (symbol, source_run_id, source_preset_id, source_preset_revision)
);

CREATE TABLE IF NOT EXISTS discovery_screen_alert_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preset_id INTEGER NOT NULL CHECK (preset_id > 0),
    current_run_id INTEGER NOT NULL CHECK (current_run_id > 0),
    previous_run_id INTEGER NOT NULL CHECK (previous_run_id > 0),
    preset_revision INTEGER NOT NULL CHECK (preset_revision > 0),
    event_digest TEXT NOT NULL CHECK (
        length(event_digest) = 64 AND event_digest NOT GLOB '*[^0-9a-f]*'
    ),
    entered_symbols_json TEXT NOT NULL CHECK (json_valid(entered_symbols_json)),
    exited_symbols_json TEXT NOT NULL CHECK (json_valid(exited_symbols_json)),
    suppressed_unrankable_symbols_json TEXT NOT NULL CHECK (json_valid(suppressed_unrankable_symbols_json)),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (preset_id) REFERENCES discovery_preset(id) ON DELETE CASCADE,
    FOREIGN KEY (current_run_id) REFERENCES market_scan_run(id) ON DELETE CASCADE,
    FOREIGN KEY (previous_run_id) REFERENCES market_scan_run(id) ON DELETE CASCADE,
    UNIQUE (preset_id, current_run_id, previous_run_id, preset_revision, event_digest)
);

CREATE INDEX IF NOT EXISTS idx_discovery_preset_updated
    ON discovery_preset(updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_queue_source_symbol_time
    ON discovery_research_queue_source(symbol, enqueued_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_queue_source_run_preset
    ON discovery_research_queue_source(source_run_id, source_preset_id);
CREATE INDEX IF NOT EXISTS idx_discovery_screen_alert_event_preset_time
    ON discovery_screen_alert_event(preset_id, created_at DESC, id DESC);

COMMIT;
"""


def apply_discovery_schema(conn: sqlite3.Connection) -> None:
    if _needs_v2_migration(conn):
        _migrate_discovery_schema_v2(conn)
    conn.executescript(DISCOVERY_SCHEMA_SQL)


def _needs_v2_migration(conn: sqlite3.Connection) -> bool:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'discovery_preset'"
    ).fetchone()
    if table is None:
        return False
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(discovery_preset)")}
    return "column_view" not in columns


def _migrate_discovery_schema_v2(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        BEGIN IMMEDIATE;

        ALTER TABLE discovery_research_queue_source RENAME TO discovery_research_queue_source_v1;
        ALTER TABLE discovery_preset RENAME TO discovery_preset_v1;

        CREATE TABLE discovery_preset (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE
                CHECK (length(trim(name)) BETWEEN 1 AND 80),
            schema_version INTEGER NOT NULL DEFAULT 2 CHECK (schema_version IN (1, 2)),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            criteria_json TEXT NOT NULL CHECK (json_valid(criteria_json)),
            sort_json TEXT NOT NULL CHECK (json_valid(sort_json)),
            column_view TEXT NOT NULL DEFAULT 'overview'
                CHECK (column_view IN ('overview', 'trend', 'liquidity', 'risk', 'research')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        INSERT INTO discovery_preset (
            id, name, schema_version, revision, criteria_json, sort_json,
            column_view, created_at, updated_at
        )
        SELECT id, name, schema_version, revision, criteria_json, sort_json,
               'overview', created_at, updated_at
        FROM discovery_preset_v1;

        CREATE TABLE discovery_research_queue_source (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            source_run_id INTEGER NOT NULL CHECK (source_run_id > 0),
            source_preset_id INTEGER NOT NULL CHECK (source_preset_id > 0),
            source_preset_revision INTEGER NOT NULL CHECK (source_preset_revision > 0),
            source_preset_name TEXT NOT NULL
                CHECK (length(trim(source_preset_name)) BETWEEN 1 AND 80),
            preset_schema_version INTEGER NOT NULL CHECK (preset_schema_version IN (1, 2)),
            preset_snapshot_json TEXT NOT NULL CHECK (json_valid(preset_snapshot_json)),
            enqueued_at TEXT NOT NULL CHECK (length(trim(enqueued_at)) > 0),
            FOREIGN KEY (symbol) REFERENCES watchlist(symbol) ON DELETE CASCADE,
            UNIQUE (symbol, source_run_id, source_preset_id, source_preset_revision)
        );

        INSERT INTO discovery_research_queue_source (
            id, symbol, source_run_id, source_preset_id, source_preset_revision,
            source_preset_name, preset_schema_version, preset_snapshot_json, enqueued_at
        )
        SELECT id, symbol, source_run_id, source_preset_id, source_preset_revision,
               source_preset_name, preset_schema_version, preset_snapshot_json, enqueued_at
        FROM discovery_research_queue_source_v1;

        DROP TABLE discovery_research_queue_source_v1;
        DROP TABLE discovery_preset_v1;
        COMMIT;
        """
    )


__all__ = ["DISCOVERY_SCHEMA_SQL", "apply_discovery_schema"]
