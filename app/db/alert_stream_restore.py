"""Allow only notification metadata writes in an already verified private restore copy."""

from __future__ import annotations

from contextlib import closing
from functools import partial
from pathlib import Path
import sqlite3

from app.db.alert_stream import (
    ALERT_STREAM_TABLE,
    AlertStreamState,
    alert_stream_table_exists,
    rotate_alert_stream_state,
    validate_existing_alert_stream_state,
)


def rotate_private_restore_stream(path: Path) -> AlertStreamState:
    """The caller must verify and privately own the database before calling this."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        validate_existing_alert_stream_state(conn)
        allow_create = not alert_stream_table_exists(conn)
        conn.set_authorizer(partial(_authorize_stream_restore, allow_create=allow_create))
        try:
            with conn:
                return rotate_alert_stream_state(conn)
        finally:
            conn.set_authorizer(None)


def _authorize_stream_restore(
    action: int,
    name: str | None,
    detail: str | None,
    database: str | None,
    trigger: str | None,
    *,
    allow_create: bool,
) -> int:
    if database not in {None, "main"} or trigger is not None:
        return sqlite3.SQLITE_DENY
    if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_TRANSACTION}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and detail in {"max", "typeof", "length", "glob"}:
        return sqlite3.SQLITE_OK
    if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
        if name == ALERT_STREAM_TABLE:
            return sqlite3.SQLITE_OK
        if allow_create and name == "sqlite_master" and action != sqlite3.SQLITE_DELETE:
            return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_CREATE_TABLE and allow_create and name == ALERT_STREAM_TABLE:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY
