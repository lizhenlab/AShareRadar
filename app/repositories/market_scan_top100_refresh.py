from __future__ import annotations

import sqlite3

from app.models.market_scan import (
    MARKET_SCAN_TOP100_REFRESH_LIMIT,
    MARKET_SCAN_TOP100_REFRESH_SCOPE,
)
from app.repositories.market_scan_lifecycle_support import _required_lastrowid


def prepare_top100_refresh_snapshot(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    *,
    source_run_id: int,
    rule_version: str,
    as_of: str,
    data_date: str,
    quote_date: str,
    limit: int,
    stamp: str,
) -> int:
    bounded_limit = max(1, min(MARKET_SCAN_TOP100_REFRESH_LIMIT, int(limit)))
    rows = _top100_refresh_rows(conn, source_run_id, bounded_limit)
    if not rows:
        raise ValueError(f"扫描批次 {source_run_id} 没有可快速更新的有效排名")
    refresh_run_id = _insert_top100_refresh_run(
        conn,
        source,
        source_run_id=source_run_id,
        rule_version=rule_version,
        as_of=as_of,
        data_date=data_date,
        quote_date=quote_date,
        result_count=len(rows),
        stamp=stamp,
    )
    _insert_top100_refresh_results(conn, refresh_run_id, rows, stamp=stamp)
    return refresh_run_id


def _top100_refresh_rows(
    conn: sqlite3.Connection,
    source_run_id: int,
    limit: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT symbol, code, market, name, industry, list_date,
               is_st, is_new, metadata_source
        FROM market_scan_result
        WHERE run_id = ? AND status = 'success' AND rank IS NOT NULL
        ORDER BY rank ASC, symbol ASC
        LIMIT ?
        """,
        (source_run_id, limit),
    ).fetchall()


def _insert_top100_refresh_run(
    conn: sqlite3.Connection,
    source: sqlite3.Row,
    *,
    source_run_id: int,
    rule_version: str,
    as_of: str,
    data_date: str,
    quote_date: str,
    result_count: int,
    stamp: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO market_scan_run (
            retry_of_run_id, status, trigger, mode, rule_version, as_of,
            data_date, quote_date, scope, stock_pool_source, total_count,
            excluded_count, processed_count, success_count, retry_count,
            created_at, updated_at, message
        ) VALUES (?, 'queued', 'retry', ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?)
        """,
        (
            source_run_id,
            source["mode"],
            rule_version,
            as_of,
            data_date,
            quote_date,
            MARKET_SCAN_TOP100_REFRESH_SCOPE,
            f"top100-source-run:{source_run_id}",
            result_count,
            stamp,
            stamp,
            f"等待快速更新 TOP{result_count} 评分（源批次 #{source_run_id}）",
        ),
    )
    return _required_lastrowid(cursor, operation="创建 TOP100 快速更新批次")


def _insert_top100_refresh_results(
    conn: sqlite3.Connection,
    refresh_run_id: int,
    rows: list[sqlite3.Row],
    *,
    stamp: str,
) -> None:
    conn.executemany(
        """
        INSERT INTO market_scan_result (
            run_id, symbol, code, market, name, industry, list_date,
            is_st, is_new, metadata_source, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            (
                refresh_run_id,
                row["symbol"],
                row["code"],
                row["market"],
                row["name"],
                row["industry"],
                row["list_date"],
                row["is_st"],
                row["is_new"],
                row["metadata_source"],
                stamp,
            )
            for row in rows
        ),
    )


__all__ = ["prepare_top100_refresh_snapshot"]
