from __future__ import annotations

import sqlite3

from app.db.advice_review_schema import ADVICE_REVIEW_SCHEMA_SQL, apply_advice_review_compat_schema
from app.db.discovery_schema import apply_discovery_schema
from app.db.paper_trading_schema import apply_paper_trading_schema
from app.db.strategy_lab_schema import apply_strategy_lab_schema
from app.db.schema_definitions import SCHEMA_SQL
from app.db.schema_migrations import (
    COMPAT_COLUMNS,
    apply_compat_migrations,
    apply_compat_schema,
    ensure_column,
    ensure_compat_indexes,
    run_once,
)


def initialize_schema(
    conn: sqlite3.Connection,
    *,
    legacy_audit_timezone: str = "Asia/Shanghai",
) -> None:
    conn.executescript(SCHEMA_SQL)
    apply_discovery_schema(conn)
    apply_compat_schema(conn, legacy_audit_timezone=legacy_audit_timezone)
    conn.executescript(ADVICE_REVIEW_SCHEMA_SQL)
    apply_advice_review_compat_schema(
        conn,
        legacy_audit_timezone=legacy_audit_timezone,
    )
    apply_paper_trading_schema(conn)
    apply_strategy_lab_schema(conn)


__all__ = [
    "COMPAT_COLUMNS",
    "SCHEMA_SQL",
    "apply_compat_migrations",
    "apply_compat_schema",
    "ensure_column",
    "ensure_compat_indexes",
    "initialize_schema",
    "run_once",
]
