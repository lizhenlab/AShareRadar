from __future__ import annotations

import hashlib
import json

from app.db.advice_review_evidence_schema import apply_advice_review_evidence_schema
from app.db.schema_migrations import ADVICE_REVIEW_AUDIT_UTC_SCHEMA_VERSION


ADVICE_REVIEW_SCHEMA_VERSION = "20260716_advice_review_v1"
ADVICE_REVIEW_PROVENANCE_SCHEMA_VERSION = "20260717_advice_review_price_provenance_v2"
ADVICE_REVIEW_LEDGER_SCHEMA_VERSION = "20260813_advice_review_immutable_ledger_v4"
WATCHLIST_SCAN_HISTORY_SCHEMA_VERSION = "20260730_watchlist_scan_history_v1"

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
    trigger_basis TEXT NOT NULL DEFAULT 'daily_high_gte_target_price'
        CHECK (trigger_basis = 'daily_high_gte_target_price'),
    invalidation_basis TEXT NOT NULL DEFAULT 'daily_low_lte_stop_price'
        CHECK (invalidation_basis = 'daily_low_lte_stop_price'),
    target_price REAL NOT NULL CHECK (target_price > 0),
    stop_price REAL NOT NULL CHECK (stop_price > 0),
    horizon_days INTEGER NOT NULL CHECK (horizon_days BETWEEN 1 AND 60),
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    plan_payload_digest TEXT NOT NULL DEFAULT 'legacy-unverified',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
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
    trigger_basis TEXT NOT NULL DEFAULT 'daily_high_gte_target_price'
        CHECK (trigger_basis = 'daily_high_gte_target_price'),
    invalidation_basis TEXT NOT NULL DEFAULT 'daily_low_lte_stop_price'
        CHECK (invalidation_basis = 'daily_low_lte_stop_price'),
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
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    plan_payload_digest TEXT NOT NULL DEFAULT 'legacy-unverified',
    input_digest TEXT NOT NULL DEFAULT 'legacy-unverified',
    result_digest TEXT NOT NULL DEFAULT 'legacy-unverified',
    evidence_contract_version TEXT NOT NULL DEFAULT 'legacy-unverified',
    source_window_digest TEXT NOT NULL DEFAULT 'legacy-unverified',
    source_session_count INTEGER NOT NULL DEFAULT 0 CHECK (source_session_count >= 0),
    expected_session_count INTEGER NOT NULL DEFAULT 0 CHECK (expected_session_count >= 0),
    observation_basis TEXT NOT NULL DEFAULT 'gross_close_and_barrier_observation'
        CHECK (observation_basis = 'gross_close_and_barrier_observation'),
    UNIQUE(plan_id, plan_revision, as_of, rule_version, attempt),
    UNIQUE(plan_id, plan_revision, as_of, rule_version, input_digest, result_digest),
    FOREIGN KEY(plan_id) REFERENCES advice_review_plan(id) ON DELETE RESTRICT,
    FOREIGN KEY(advice_id) REFERENCES advice_history(id) ON DELETE RESTRICT
)
"""

ADVICE_REVIEW_PLAN_REVISION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS advice_review_plan_revision (
    plan_id INTEGER NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    payload_json TEXT NOT NULL CHECK (length(payload_json) > 2),
    payload_digest TEXT NOT NULL CHECK (
        length(payload_digest) = 64
        AND payload_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, revision),
    FOREIGN KEY(plan_id) REFERENCES advice_review_plan(id) ON DELETE RESTRICT
)
"""

WATCHLIST_SCAN_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS watchlist_scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    universe_kind TEXT NOT NULL CHECK (universe_kind IN ('watchlist', 'symbols')),
    as_of TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    conditions_json TEXT NOT NULL,
    universe_count INTEGER NOT NULL CHECK (universe_count >= 0),
    success_count INTEGER NOT NULL CHECK (success_count >= 0),
    matched_count INTEGER NOT NULL CHECK (matched_count >= 0),
    missing_count INTEGER NOT NULL CHECK (missing_count >= 0),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

ADVICE_REVIEW_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_advice_review_plan_symbol_updated
    ON advice_review_plan(symbol, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_advice_review_result_plan_evaluated
    ON advice_review_result(plan_id, plan_revision, evaluated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_advice_review_result_advice
    ON advice_review_result(advice_id, evaluated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_advice_review_plan_revision_digest
    ON advice_review_plan_revision(payload_digest);
CREATE INDEX IF NOT EXISTS idx_watchlist_scan_history_created
    ON watchlist_scan_history(created_at DESC, id DESC);
"""

ADVICE_REVIEW_LEDGER_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_advice_review_result_observation
    ON advice_review_result(plan_id, plan_revision, as_of DESC, attempt DESC, id DESC)
"""

ADVICE_REVIEW_SCHEMA_SQL = f"""
BEGIN IMMEDIATE;
{ADVICE_REVIEW_PLAN_TABLE_SQL};
{ADVICE_REVIEW_PLAN_REVISION_TABLE_SQL};
{ADVICE_REVIEW_RESULT_TABLE_SQL};
{WATCHLIST_SCAN_HISTORY_TABLE_SQL};
{ADVICE_REVIEW_INDEX_SQL};
INSERT OR IGNORE INTO schema_migration (name) VALUES ('{ADVICE_REVIEW_SCHEMA_VERSION}');
INSERT OR IGNORE INTO schema_migration (name) VALUES ('{WATCHLIST_SCAN_HISTORY_SCHEMA_VERSION}');
INSERT OR IGNORE INTO schema_migration (name) VALUES ('{ADVICE_REVIEW_LEDGER_SCHEMA_VERSION}');
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


def apply_advice_review_compat_schema(
    conn,
    *,
    legacy_audit_timezone: str = "Asia/Shanghai",
) -> None:
    from app.db.schema_migrations import normalize_legacy_audit_timestamps

    requires_result_rebuild = _result_requires_ledger_rebuild(conn)
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    transaction_started = False
    try:
        if requires_result_rebuild and foreign_keys_enabled:
            conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        transaction_started = True
        _apply_advice_review_compat_transaction(
            conn,
            legacy_audit_timezone=legacy_audit_timezone,
            normalize_legacy_audit_timestamps=normalize_legacy_audit_timestamps,
        )
    except BaseException:
        if transaction_started:
            conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        if requires_result_rebuild and foreign_keys_enabled:
            conn.execute("PRAGMA foreign_keys = ON")


def _apply_advice_review_compat_transaction(
    conn,
    *,
    legacy_audit_timezone: str,
    normalize_legacy_audit_timestamps,
) -> None:
    _ensure_column(conn, "advice_review_plan", "deleted_at", "TEXT")
    _ensure_column(
        conn,
        "advice_review_plan",
        "plan_payload_digest",
        "TEXT NOT NULL DEFAULT 'legacy-unverified'",
    )
    for column, definition in _PLAN_PROVENANCE_COLUMNS.items():
        _ensure_column(conn, "advice_review_plan", column, definition)
    for column, definition in _RESULT_PROVENANCE_COLUMNS.items():
        _ensure_column(conn, "advice_review_result", column, definition)
    apply_advice_review_evidence_schema(conn)
    _apply_immutable_ledger_schema(conn)
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("复盘账本升级后外键检查失败")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migration (name) VALUES (?)",
        (ADVICE_REVIEW_PROVENANCE_SCHEMA_VERSION,),
    )
    _normalize_review_audit_timestamps_once(
        conn,
        legacy_audit_timezone=legacy_audit_timezone,
        normalize_legacy_audit_timestamps=normalize_legacy_audit_timestamps,
    )


def _normalize_review_audit_timestamps_once(
    conn,
    *,
    legacy_audit_timezone: str,
    normalize_legacy_audit_timestamps,
) -> None:
    audit_migration = conn.execute(
        "SELECT 1 FROM schema_migration WHERE name = ?",
        (ADVICE_REVIEW_AUDIT_UTC_SCHEMA_VERSION,),
    ).fetchone()
    if audit_migration is not None:
        return
    normalize_legacy_audit_timestamps(
        conn,
        {
            "advice_review_plan": ("created_at", "updated_at"),
            "advice_review_plan_revision": ("created_at",),
            "advice_review_result": ("evaluated_at",),
        },
        legacy_audit_timezone=legacy_audit_timezone,
    )
    conn.execute(
        "INSERT INTO schema_migration (name) VALUES (?)",
        (ADVICE_REVIEW_AUDIT_UTC_SCHEMA_VERSION,),
    )


def apply_advice_review_schema(
    conn,
    *,
    legacy_audit_timezone: str = "Asia/Shanghai",
) -> None:
    """Create fresh review tables or atomically migrate an existing schema."""

    existing = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'advice_review_plan'"
    ).fetchone()
    if existing is not None:
        apply_advice_review_compat_schema(
            conn,
            legacy_audit_timezone=legacy_audit_timezone,
        )
        return
    conn.executescript(ADVICE_REVIEW_SCHEMA_SQL)
    apply_advice_review_compat_schema(
        conn,
        legacy_audit_timezone=legacy_audit_timezone,
    )


def _ensure_column(conn, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _apply_immutable_ledger_schema(conn) -> None:
    conn.execute(ADVICE_REVIEW_PLAN_REVISION_TABLE_SQL)
    result_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(advice_review_result)")}
    if "attempt" not in result_columns:
        _rebuild_result_as_append_only(conn)
    else:
        for column, definition in _RESULT_LEDGER_COLUMNS.items():
            _ensure_column(conn, "advice_review_result", column, definition)
    _backfill_current_plan_revisions(conn)
    for statement in ADVICE_REVIEW_INDEX_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute(ADVICE_REVIEW_LEDGER_INDEX_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migration (name) VALUES (?)",
        (ADVICE_REVIEW_LEDGER_SCHEMA_VERSION,),
    )


_RESULT_LEDGER_COLUMNS = {
    "plan_payload_digest": "TEXT NOT NULL DEFAULT 'legacy-unverified'",
    "input_digest": "TEXT NOT NULL DEFAULT 'legacy-unverified'",
    "result_digest": "TEXT NOT NULL DEFAULT 'legacy-unverified'",
    "evidence_contract_version": "TEXT NOT NULL DEFAULT 'legacy-unverified'",
    "source_window_digest": "TEXT NOT NULL DEFAULT 'legacy-unverified'",
    "source_session_count": "INTEGER NOT NULL DEFAULT 0 CHECK (source_session_count >= 0)",
    "expected_session_count": "INTEGER NOT NULL DEFAULT 0 CHECK (expected_session_count >= 0)",
    "observation_basis": (
        "TEXT NOT NULL DEFAULT 'gross_close_and_barrier_observation' "
        "CHECK (observation_basis = 'gross_close_and_barrier_observation')"
    ),
}


def _rebuild_result_as_append_only(conn) -> None:
    conn.execute("ALTER TABLE advice_review_result RENAME TO advice_review_result_legacy_v3")
    conn.execute(ADVICE_REVIEW_RESULT_TABLE_SQL)
    legacy_columns = [
        str(row[1])
        for row in conn.execute("PRAGMA table_info(advice_review_result_legacy_v3)").fetchall()
    ]
    retained = [column for column in legacy_columns if column not in {"attempt", "plan_payload_digest", "input_digest", "result_digest"}]
    columns = ", ".join(retained)
    conn.execute(
        f"""
        INSERT INTO advice_review_result ({columns}, attempt, plan_payload_digest, input_digest, result_digest)
        SELECT {columns}, 1, 'legacy-unverified', 'legacy-unverified', 'legacy-unverified'
        FROM advice_review_result_legacy_v3
        """
    )
    conn.execute("DROP TABLE advice_review_result_legacy_v3")


def _result_requires_ledger_rebuild(conn) -> bool:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    if "advice_review_result" not in tables:
        return False
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(advice_review_result)")}
    return "attempt" not in columns


def _backfill_current_plan_revisions(conn) -> None:
    rows = conn.execute("SELECT * FROM advice_review_plan ORDER BY id").fetchall()
    for row in rows:
        plan_id = int(row["id"])
        revision = int(row["revision"])
        exists = conn.execute(
            "SELECT payload_digest FROM advice_review_plan_revision WHERE plan_id = ? AND revision = ?",
            (plan_id, revision),
        ).fetchone()
        if exists is not None:
            conn.execute(
                "UPDATE advice_review_plan SET plan_payload_digest = ? WHERE id = ?",
                (str(exists["payload_digest"]), plan_id),
            )
            continue
        payload = _legacy_plan_payload(row)
        payload_json = _canonical_json(payload)
        payload_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        conn.execute(
            """
            INSERT INTO advice_review_plan_revision (
                plan_id, revision, payload_json, payload_digest, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                revision,
                payload_json,
                payload_digest,
                str(row["updated_at"] or row["created_at"]),
            ),
        )
        conn.execute(
            "UPDATE advice_review_plan SET plan_payload_digest = ? WHERE id = ?",
            (payload_digest, plan_id),
        )


def _legacy_plan_payload(row) -> dict[str, object]:
    try:
        evidence_refs = json.loads(str(row["evidence_refs_json"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        evidence_refs = {"status": "corrupt", "raw": str(row["evidence_refs_json"] or "")}
    return {
        "advice_id": int(row["advice_id"]),
        "evidence_refs": evidence_refs,
        "horizon_days": int(row["horizon_days"]),
        "hypothesis": str(row["hypothesis"]),
        "invalidation_basis": str(row["invalidation_basis"]),
        "invalidation_condition": str(row["invalidation_condition"]),
        "snapshot": {
            "adjustment_mode": str(row["snapshot_adjustment_mode"] or "unknown"),
            "anchor_close": row["snapshot_anchor_close"],
            "anchor_date": row["snapshot_anchor_date"],
            "contract_version": str(row["snapshot_contract_version"] or "unknown"),
            "data_version": str(row["snapshot_data_version"] or "unknown"),
            "market_time": str(row["snapshot_market_time"]),
            "price": float(row["snapshot_price"]),
        },
        "stop_price": float(row["stop_price"]),
        "symbol": str(row["symbol"]),
        "target_price": float(row["target_price"]),
        "trigger_basis": str(row["trigger_basis"]),
        "trigger_condition": str(row["trigger_condition"]),
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


__all__ = [
    "ADVICE_REVIEW_AUDIT_UTC_SCHEMA_VERSION",
    "ADVICE_REVIEW_INDEX_SQL",
    "ADVICE_REVIEW_PROVENANCE_SCHEMA_VERSION",
    "ADVICE_REVIEW_LEDGER_SCHEMA_VERSION",
    "ADVICE_REVIEW_PLAN_REVISION_TABLE_SQL",
    "ADVICE_REVIEW_PLAN_TABLE_SQL",
    "ADVICE_REVIEW_RESULT_TABLE_SQL",
    "ADVICE_REVIEW_SCHEMA_SQL",
    "apply_advice_review_schema",
    "ADVICE_REVIEW_SCHEMA_VERSION",
    "WATCHLIST_SCAN_HISTORY_SCHEMA_VERSION",
    "WATCHLIST_SCAN_HISTORY_TABLE_SQL",
    "apply_advice_review_compat_schema",
]
