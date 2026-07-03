from __future__ import annotations

import sqlite3

from app.db.schema_definitions import SCHEMA_SQL
from app.db.schema_migrations import (
    COMPAT_COLUMNS,
    apply_compat_migrations,
    apply_compat_schema,
    ensure_column,
    ensure_compat_indexes,
    run_once,
)


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    apply_compat_schema(conn)


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
