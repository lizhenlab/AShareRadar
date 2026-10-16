"""Durable manual-request identities, separate from disposable task-run history."""

from __future__ import annotations

import sqlite3


def apply_task_request_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_run_request (
            request_id TEXT PRIMARY KEY,
            requested_task TEXT,
            status TEXT NOT NULL CHECK(status IN ('accepted', 'running', 'completed', 'failed', 'interrupted')),
            created_at TEXT NOT NULL,
            finished_at TEXT,
            messages_json TEXT NOT NULL DEFAULT '[]'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_run_request_item (
            request_id TEXT NOT NULL REFERENCES task_run_request(request_id) ON DELETE CASCADE,
            task_name TEXT NOT NULL,
            task_run_id INTEGER REFERENCES task_run(id) ON DELETE SET NULL,
            PRIMARY KEY (request_id, task_name)
        )
    """)


__all__ = ["apply_task_request_schema"]
