"""Database graph authorization for bounded market-scan retention."""

from __future__ import annotations

import sqlite3

from app.db.market_scan_integrity import (
    PUBLISHED_MARKET_SCAN_STATUSES,
    delete_verified_market_scan_snapshots,
)
from app.repositories.runtime_research_artifact_retention import (
    MarketScanArtifactProtection,
    RuntimeCleanupIntegrityError,
)


def market_scan_cleanup_candidate_ids(
    conn: sqlite3.Connection,
    overflow_sql: str,
    limit: int,
    artifacts: MarketScanArtifactProtection,
) -> tuple[int, ...]:
    rows = conn.execute(
        f"""
        SELECT overflow.retention_key, overflow.status
        FROM ({overflow_sql}) AS overflow
        ORDER BY overflow.retention_key ASC
        """,
        {"retention_limit": limit},
    ).fetchall()
    candidates = {int(row[0]) for row in rows}
    published = {
        int(row[0])
        for row in rows
        if str(row[1]) in PUBLISHED_MARKET_SCAN_STATUSES
    }
    protected = _database_reference_ids(conn, candidates)
    protected.update(published.intersection(artifacts.run_ids))
    protected = _retry_graph_protected_ids(conn, candidates, protected)
    return tuple(sorted(candidates - protected))


def delete_market_scan_candidates(
    conn: sqlite3.Connection,
    run_ids: tuple[int, ...],
) -> int:
    if not run_ids:
        return 0
    rows = conn.execute(
        f"SELECT id, status FROM market_scan_run WHERE id IN ({_placeholders(run_ids)})",
        run_ids,
    ).fetchall()
    status_by_id = {int(row[0]): str(row[1]) for row in rows}
    if set(status_by_id) != set(run_ids):
        raise RuntimeCleanupIntegrityError("全市场扫描清理候选在事务内发生变化")
    published = tuple(
        run_id
        for run_id in run_ids
        if status_by_id[run_id] in PUBLISHED_MARKET_SCAN_STATUSES
    )
    unpublished = tuple(run_id for run_id in run_ids if run_id not in published)
    deleted = delete_verified_market_scan_snapshots(conn, published)
    for batch in _id_batches(unpublished):
        cursor = conn.execute(
            f"DELETE FROM market_scan_run WHERE id IN ({_placeholders(batch)})",
            batch,
        )
        deleted += max(0, int(cursor.rowcount))
    if deleted != len(run_ids):
        raise RuntimeCleanupIntegrityError("全市场扫描清理候选未被完整删除")
    return deleted


def _database_reference_ids(
    conn: sqlite3.Connection,
    candidates: set[int],
) -> set[int]:
    protected: set[int] = set()
    for table, column in _foreign_key_references(conn):
        if table == "market_scan_probability_capture_outbox":
            continue
        protected.update(_column_reference_ids(conn, table, column, candidates))
    protected.update(_active_probability_capture_ids(conn, candidates))
    for table, column in (
        ("discovery_research_queue_source", "source_run_id"),
        ("strategy_execution", "market_scan_run_id"),
        ("strategy_schedule", "last_market_scan_run_id"),
        ("strategy_schedule_run", "market_scan_run_id"),
    ):
        protected.update(_column_reference_ids(conn, table, column, candidates))
    return protected


def _active_probability_capture_ids(
    conn: sqlite3.Connection,
    candidates: set[int],
) -> set[int]:
    rows = conn.execute(
        """
        SELECT run_id
        FROM market_scan_probability_capture_outbox
        WHERE status IN ('pending', 'processing')
        """
    ).fetchall()
    return candidates.intersection(int(row[0]) for row in rows)


def _foreign_key_references(
    conn: sqlite3.Connection,
) -> tuple[tuple[str, str], ...]:
    references: set[tuple[str, str]] = set()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    for table_row in tables:
        table = str(table_row[0])
        foreign_keys = conn.execute(
            f"PRAGMA foreign_key_list({_quote_identifier(table)})"
        ).fetchall()
        for foreign_key in foreign_keys:
            reference = table, str(foreign_key[3])
            if str(foreign_key[2]) != "market_scan_run":
                continue
            if reference in {
                ("market_scan_result", "run_id"),
                ("market_scan_run", "retry_of_run_id"),
            }:
                continue
            references.add(reference)
    return tuple(sorted(references))


def _column_reference_ids(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    candidates: set[int],
) -> set[int]:
    columns = {
        str(row[1])
        for row in conn.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        ).fetchall()
    }
    if column not in columns:
        return set()
    rows = conn.execute(
        f"SELECT DISTINCT {_quote_identifier(column)} "
        f"FROM {_quote_identifier(table)} "
        f"WHERE {_quote_identifier(column)} IS NOT NULL"
    ).fetchall()
    return candidates.intersection(int(row[0]) for row in rows)


def _retry_graph_protected_ids(
    conn: sqlite3.Connection,
    candidates: set[int],
    initially_protected: set[int],
) -> set[int]:
    parent_by_child: dict[int, int] = {}
    rows = conn.execute(
        "SELECT id, retry_of_run_id FROM market_scan_run WHERE retry_of_run_id IS NOT NULL"
    ).fetchall()
    for row in rows:
        child, parent = int(row[0]), int(row[1])
        parent_by_child[child] = parent
    protected = set(initially_protected)
    pending = list((parent_by_child.keys() - candidates) | protected)
    visited: set[int] = set()
    while pending:
        child = pending.pop()
        if child in visited:
            continue
        visited.add(child)
        next_parent = parent_by_child.get(child)
        if next_parent is None:
            continue
        if next_parent in candidates:
            protected.add(next_parent)
        pending.append(next_parent)
    return protected


def _placeholders(values: tuple[int, ...]) -> str:
    return ", ".join("?" for _ in values)


def _id_batches(values: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    return tuple(values[start : start + 900] for start in range(0, len(values), 900))


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


__all__ = ["delete_market_scan_candidates", "market_scan_cleanup_candidate_ids"]
