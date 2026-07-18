from __future__ import annotations


ADVICE_REVIEW_SCHEMA_VERSION = "20260716_advice_review_v1"
ADVICE_REVIEW_PROVENANCE_SCHEMA_VERSION = "20260717_advice_review_price_provenance_v2"

ADVICE_REVIEW_PLAN_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS advice_review_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    advice_id INTEGER NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    snapshot_market_time TEXT NOT NULL,
    snapshot_price REAL NOT NULL CHECK (snapshot_price > 0),
    snapshot_adjustment_mode TEXT NOT NULL DEFAULT 'unknown',
    snapshot_anchor_date TEXT,
    snapshot_anchor_close REAL,
    snapshot_data_version TEXT NOT NULL DEFAULT 'unknown',
    snapshot_contract_version TEXT NOT NULL DEFAULT 'unknown',
    hypothesis TEXT NOT NULL CHECK (length(trim(hypothesis)) > 0),
    trigger_condition TEXT NOT NULL CHECK (length(trim(trigger_condition)) > 0),
    invalidation_condition TEXT NOT NULL CHECK (length(trim(invalidation_condition)) > 0),
    target_price REAL NOT NULL CHECK (target_price > 0),
    stop_price REAL NOT NULL CHECK (stop_price > 0),
    horizon_days INTEGER NOT NULL CHECK (horizon_days BETWEEN 1 AND 60),
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (target_price > snapshot_price AND snapshot_price > stop_price),
    FOREIGN KEY(advice_id) REFERENCES advice_history(id) ON DELETE RESTRICT
)
"""

ADVICE_REVIEW_RESULT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS advice_review_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    plan_revision INTEGER NOT NULL CHECK (plan_revision >= 1),
    advice_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    snapshot_market_time TEXT NOT NULL,
    as_of TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'insufficient', 'evaluated')),
    conclusion TEXT NOT NULL CHECK (
        conclusion IN (
            'pending',
            'insufficient_data',
            'target_hit',
            'stop_hit',
            'target_stop_ambiguous',
            'horizon_gain',
            'horizon_loss',
            'horizon_flat'
        )
    ),
    rule_version TEXT NOT NULL,
    snapshot_adjustment_mode TEXT NOT NULL DEFAULT 'unknown',
    snapshot_anchor_date TEXT,
    snapshot_anchor_close REAL,
    snapshot_data_version TEXT NOT NULL DEFAULT 'unknown',
    snapshot_contract_version TEXT NOT NULL DEFAULT 'unknown',
    evaluation_adjustment_mode TEXT NOT NULL DEFAULT 'unknown',
    evaluation_data_version TEXT NOT NULL DEFAULT 'unknown',
    evaluation_contract_version TEXT NOT NULL DEFAULT 'unknown',
    anchor_evaluation_close REAL,
    price_scale_factor REAL,
    normalized_entry_price REAL,
    normalized_target_price REAL,
    normalized_stop_price REAL,
    entry_price REAL NOT NULL CHECK (entry_price > 0),
    target_price REAL NOT NULL CHECK (target_price > 0),
    stop_price REAL NOT NULL CHECK (stop_price > 0),
    horizon_days INTEGER NOT NULL CHECK (horizon_days BETWEEN 1 AND 60),
    visible_bar_count INTEGER NOT NULL CHECK (visible_bar_count >= 0),
    visible_start_date TEXT,
    visible_end_date TEXT,
    available_forward_days INTEGER NOT NULL CHECK (available_forward_days >= 0),
    forward_start_date TEXT,
    forward_end_date TEXT,
    return_pct REAL,
    max_favorable_excursion_pct REAL,
    max_adverse_excursion_pct REAL,
    target_hit INTEGER NOT NULL CHECK (target_hit IN (0, 1)),
    target_hit_date TEXT,
    stop_hit INTEGER NOT NULL CHECK (stop_hit IN (0, 1)),
    stop_hit_date TEXT,
    UNIQUE(plan_id, plan_revision, as_of, rule_version),
    FOREIGN KEY(plan_id) REFERENCES advice_review_plan(id) ON DELETE CASCADE,
    FOREIGN KEY(advice_id) REFERENCES advice_history(id) ON DELETE RESTRICT
)
"""

ADVICE_REVIEW_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_advice_review_plan_symbol_updated
    ON advice_review_plan(symbol, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_advice_review_result_plan_evaluated
    ON advice_review_result(plan_id, plan_revision, evaluated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_advice_review_result_advice
    ON advice_review_result(advice_id, evaluated_at DESC, id DESC)
"""

ADVICE_REVIEW_SCHEMA_SQL = f"""
BEGIN IMMEDIATE;
{ADVICE_REVIEW_PLAN_TABLE_SQL};
{ADVICE_REVIEW_RESULT_TABLE_SQL};
{ADVICE_REVIEW_INDEX_SQL};
INSERT OR IGNORE INTO schema_migration (name) VALUES ('{ADVICE_REVIEW_SCHEMA_VERSION}');
COMMIT;
"""


_PLAN_PROVENANCE_COLUMNS = {
    "snapshot_adjustment_mode": "TEXT NOT NULL DEFAULT 'unknown'",
    "snapshot_anchor_date": "TEXT",
    "snapshot_anchor_close": "REAL",
    "snapshot_data_version": "TEXT NOT NULL DEFAULT 'unknown'",
    "snapshot_contract_version": "TEXT NOT NULL DEFAULT 'unknown'",
}
_RESULT_PROVENANCE_COLUMNS = {
    **_PLAN_PROVENANCE_COLUMNS,
    "evaluation_adjustment_mode": "TEXT NOT NULL DEFAULT 'unknown'",
    "evaluation_data_version": "TEXT NOT NULL DEFAULT 'unknown'",
    "evaluation_contract_version": "TEXT NOT NULL DEFAULT 'unknown'",
    "anchor_evaluation_close": "REAL",
    "price_scale_factor": "REAL",
    "normalized_entry_price": "REAL",
    "normalized_target_price": "REAL",
    "normalized_stop_price": "REAL",
}


def apply_advice_review_compat_schema(conn) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for column, definition in _PLAN_PROVENANCE_COLUMNS.items():
            _ensure_column(conn, "advice_review_plan", column, definition)
        for column, definition in _RESULT_PROVENANCE_COLUMNS.items():
            _ensure_column(conn, "advice_review_result", column, definition)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migration (name) VALUES (?)",
            (ADVICE_REVIEW_PROVENANCE_SCHEMA_VERSION,),
        )
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _ensure_column(conn, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


__all__ = [
    "ADVICE_REVIEW_INDEX_SQL",
    "ADVICE_REVIEW_PROVENANCE_SCHEMA_VERSION",
    "ADVICE_REVIEW_PLAN_TABLE_SQL",
    "ADVICE_REVIEW_RESULT_TABLE_SQL",
    "ADVICE_REVIEW_SCHEMA_SQL",
    "ADVICE_REVIEW_SCHEMA_VERSION",
    "apply_advice_review_compat_schema",
]
