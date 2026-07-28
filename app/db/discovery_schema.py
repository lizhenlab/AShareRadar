from __future__ import annotations

import sqlite3


DISCOVERY_SCHEMA_SQL = """
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS discovery_preset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE
        CHECK (length(trim(name)) BETWEEN 1 AND 80),
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    criteria_json TEXT NOT NULL CHECK (json_valid(criteria_json)),
    sort_json TEXT NOT NULL CHECK (json_valid(sort_json)),
    created_at TEXT NOT NULL,
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
    preset_schema_version INTEGER NOT NULL CHECK (preset_schema_version = 1),
    preset_snapshot_json TEXT NOT NULL CHECK (json_valid(preset_snapshot_json)),
    enqueued_at TEXT NOT NULL CHECK (length(trim(enqueued_at)) > 0),
    FOREIGN KEY (symbol) REFERENCES watchlist(symbol) ON DELETE CASCADE,
    UNIQUE (symbol, source_run_id, source_preset_id, source_preset_revision)
);

CREATE INDEX IF NOT EXISTS idx_discovery_preset_updated
    ON discovery_preset(updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_queue_source_symbol_time
    ON discovery_research_queue_source(symbol, enqueued_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_queue_source_run_preset
    ON discovery_research_queue_source(source_run_id, source_preset_id);

COMMIT;
"""


def apply_discovery_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DISCOVERY_SCHEMA_SQL)


__all__ = ["DISCOVERY_SCHEMA_SQL", "apply_discovery_schema"]
