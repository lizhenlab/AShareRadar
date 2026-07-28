from __future__ import annotations


ADVICE_REVIEW_EXECUTABLE_BASIS_SCHEMA_VERSION = "20260728_advice_review_executable_basis_v3"

ADVICE_REVIEW_PLAN_BASIS_COLUMNS = {
    "trigger_basis": (
        "TEXT NOT NULL DEFAULT 'daily_high_gte_target_price' "
        "CHECK (trigger_basis = 'daily_high_gte_target_price')"
    ),
    "invalidation_basis": (
        "TEXT NOT NULL DEFAULT 'daily_low_lte_stop_price' "
        "CHECK (invalidation_basis = 'daily_low_lte_stop_price')"
    ),
}

ADVICE_REVIEW_RESULT_BASIS_COLUMNS = dict(ADVICE_REVIEW_PLAN_BASIS_COLUMNS)


def apply_advice_review_evidence_schema(conn) -> None:
    for column, definition in ADVICE_REVIEW_PLAN_BASIS_COLUMNS.items():
        _ensure_column(conn, "advice_review_plan", column, definition)
    for column, definition in ADVICE_REVIEW_RESULT_BASIS_COLUMNS.items():
        _ensure_column(conn, "advice_review_result", column, definition)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migration (name) VALUES (?)",
        (ADVICE_REVIEW_EXECUTABLE_BASIS_SCHEMA_VERSION,),
    )


def _ensure_column(conn, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


__all__ = [
    "ADVICE_REVIEW_EXECUTABLE_BASIS_SCHEMA_VERSION",
    "ADVICE_REVIEW_PLAN_BASIS_COLUMNS",
    "ADVICE_REVIEW_RESULT_BASIS_COLUMNS",
    "apply_advice_review_evidence_schema",
]
