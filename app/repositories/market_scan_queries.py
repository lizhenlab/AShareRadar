from __future__ import annotations

from datetime import datetime
import math
import sqlite3
from typing import Literal

from app.db.connection import SQLITE_AUDIT_EPOCH_FUNCTION
from app.models.market_scan import (
    MarketScanCoverage,
    MarketScanPublicationSummary,
    MarketScanResultItem,
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanSort,
    MarketScanSortOrder,
    MarketScanStaleCluster,
)
from app.repositories.market_scan_context import MarketScanRepositoryContext
from app.repositories.market_scan_lifecycle import ACTIVE_SCAN_STATUSES
from app.repositories.market_scan_mapping import (
    append_exact_filter,
    escaped_like,
    page_count,
    result_from_row,
    result_order_sql,
    run_from_row,
)
from app.repositories.market_scan_results import count_degraded_results, required_run_row
from app.utils.market_time import normalize_market_datetime


class MarketScanQueryMixin(MarketScanRepositoryContext):
    def run(self, run_id: int) -> MarketScanRun:
        with self._lock, self._connect() as conn:
            row = required_run_row(conn, run_id)
        return run_from_row(row)

    def active_run(self) -> MarketScanRun | None:
        placeholders = ", ".join("?" for _status in ACTIVE_SCAN_STATUSES)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM market_scan_run
                WHERE status IN ({placeholders})
                ORDER BY id DESC
                LIMIT 1
                """,
                ACTIVE_SCAN_STATUSES,
            ).fetchone()
        return run_from_row(row) if row is not None else None

    def latest_run(self) -> MarketScanRun | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM market_scan_run ORDER BY id DESC LIMIT 1").fetchone()
        return run_from_row(row) if row is not None else None

    def list_runs(self, *, page: int, page_size: int) -> MarketScanRunPage:
        offset = (page - 1) * page_size
        with self._lock, self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM market_scan_run").fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT * FROM market_scan_run
                ORDER BY {SQLITE_AUDIT_EPOCH_FUNCTION}(created_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ).fetchall()
        return MarketScanRunPage(
            items=[run_from_row(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
            page_count=page_count(total, page_size),
        )

    def pending_items(self, run_id: int) -> list[MarketScanResultItem]:
        with self._lock, self._connect() as conn:
            required_run_row(conn, run_id)
            rows = conn.execute(
                """
                SELECT * FROM market_scan_result
                WHERE run_id = ? AND status = 'pending'
                ORDER BY market ASC, code ASC, symbol ASC
                """,
                (run_id,),
            ).fetchall()
        return [result_from_row(row) for row in rows]

    def degraded_result_count(self, run_id: int) -> int:
        with self._lock, self._connect() as conn:
            required_run_row(conn, run_id)
            return count_degraded_results(conn, run_id)

    def publication_summary(self, run_id: int) -> MarketScanPublicationSummary:
        with self._lock, self._connect() as conn:
            run = required_run_row(conn, run_id)
            coverage_rows = conn.execute(
                """
                SELECT market,
                       COUNT(*) AS total_count,
                       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
                       SUM(CASE WHEN status = 'missing' THEN 1 ELSE 0 END) AS missing_count,
                       SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count
                FROM market_scan_result
                WHERE run_id = ?
                GROUP BY market
                """,
                (run_id,),
            ).fetchall()
            stale_rows = conn.execute(
                """
                SELECT data_date, COUNT(*) AS stale_count,
                       GROUP_CONCAT(DISTINCT market) AS markets
                FROM market_scan_result
                WHERE run_id = ?
                  AND status IN ('missing', 'skipped')
                  AND data_date IS NOT NULL
                  AND data_date < ?
                GROUP BY data_date
                ORDER BY stale_count DESC, data_date DESC
                """,
                (run_id, run["data_date"]),
            ).fetchall()
            timestamps = [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT quote_timestamp
                    FROM market_scan_result
                    WHERE run_id = ? AND quote_timestamp IS NOT NULL
                    ORDER BY symbol ASC
                    """,
                    (run_id,),
                ).fetchall()
            ]
        coverages = _coverage_summary(coverage_rows)
        total_count = coverages[0].total_count
        stale_cluster = _systemic_stale_cluster(stale_rows, total_count=total_count)
        (
            snapshot_started_at,
            snapshot_finished_at,
            snapshot_span_seconds,
            invalid_snapshot_timestamps,
        ) = _snapshot_span(timestamps)
        return MarketScanPublicationSummary(
            coverages=coverages,
            systemic_stale_cluster=stale_cluster,
            snapshot_started_at=snapshot_started_at,
            snapshot_finished_at=snapshot_finished_at,
            snapshot_span_seconds=snapshot_span_seconds,
            invalid_snapshot_timestamps=invalid_snapshot_timestamps,
        )

    def results_page(
        self,
        run_id: int,
        *,
        page: int,
        page_size: int,
        status: MarketScanResultStatus | None,
        market: str | None,
        industry: str | None,
        is_st: bool | None,
        is_new: bool | None,
        min_data_quality_score: int | None,
        keyword: str | None,
        sort: MarketScanSort,
        order: MarketScanSortOrder,
    ) -> MarketScanResultPage:
        clauses = ["run_id = ?"]
        params: list[object] = [run_id]
        append_exact_filter(clauses, params, "status", status)
        append_exact_filter(clauses, params, "market", market)
        append_exact_filter(clauses, params, "industry", industry)
        append_exact_filter(clauses, params, "is_st", int(is_st) if is_st is not None else None)
        append_exact_filter(clauses, params, "is_new", int(is_new) if is_new is not None else None)
        if min_data_quality_score is not None:
            clauses.append("data_quality_score >= ?")
            params.append(min_data_quality_score)
        keyword_text = " ".join((keyword or "").split()).strip()
        if keyword_text:
            like = f"%{escaped_like(keyword_text)}%"
            clauses.append("(symbol LIKE ? ESCAPE '\\' OR code LIKE ? ESCAPE '\\' " "OR name LIKE ? ESCAPE '\\')")
            params.extend((like, like, like))
        where = " AND ".join(clauses)
        order_sql = result_order_sql(sort, order)
        offset = (page - 1) * page_size
        with self._lock, self._connect() as conn:
            run_row = required_run_row(conn, run_id)
            total = int(conn.execute(f"SELECT COUNT(*) FROM market_scan_result WHERE {where}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT * FROM market_scan_result
                WHERE {where}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
                """,
                (*params, page_size, offset),
            ).fetchall()
        return MarketScanResultPage(
            run=run_from_row(run_row),
            items=[result_from_row(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
            page_count=page_count(total, page_size),
        )


def _coverage_summary(rows: list[sqlite3.Row]) -> tuple[MarketScanCoverage, ...]:
    by_market: dict[Literal["SH", "SZ", "BJ"], MarketScanCoverage] = {}
    for row in rows:
        market = _coverage_market(row["market"])
        if market is None:
            continue
        by_market[market] = MarketScanCoverage(
            market,
            total_count=int(row["total_count"] or 0),
            success_count=int(row["success_count"] or 0),
            missing_count=int(row["missing_count"] or 0),
            skipped_count=int(row["skipped_count"] or 0),
        )
    markets = (
        by_market.get("SH", MarketScanCoverage("SH", total_count=0, success_count=0)),
        by_market.get("SZ", MarketScanCoverage("SZ", total_count=0, success_count=0)),
        by_market.get("BJ", MarketScanCoverage("BJ", total_count=0, success_count=0)),
    )
    overall = MarketScanCoverage(
        "ALL",
        total_count=sum(item.total_count for item in markets),
        success_count=sum(item.success_count for item in markets),
        missing_count=sum(item.missing_count for item in markets),
        skipped_count=sum(item.skipped_count for item in markets),
    )
    return (overall, *markets)


def _coverage_market(value: object) -> Literal["SH", "SZ", "BJ"] | None:
    market = str(value)
    if market == "SH":
        return "SH"
    if market == "SZ":
        return "SZ"
    if market == "BJ":
        return "BJ"
    return None


def _systemic_stale_cluster(
    rows: list[sqlite3.Row],
    *,
    total_count: int,
) -> MarketScanStaleCluster | None:
    if not rows or total_count <= 0:
        return None
    row = rows[0]
    count = int(row["stale_count"] or 0)
    minimum_count = max(3, math.ceil(total_count * 0.05))
    if count < minimum_count:
        return None
    markets = tuple(sorted(part for part in str(row["markets"] or "").split(",") if part))
    return MarketScanStaleCluster(
        data_date=str(row["data_date"]),
        count=count,
        markets=markets,
        total_count=total_count,
    )


def _snapshot_span(
    timestamps: list[str],
) -> tuple[str | None, str | None, float | None, tuple[str, ...]]:
    parsed: list[datetime] = []
    invalid: list[str] = []
    for value in timestamps:
        snapshot_time = _parse_snapshot_time(value)
        if snapshot_time is None:
            invalid.append(value)
        else:
            parsed.append(snapshot_time)
    if not parsed:
        return None, None, None, tuple(dict.fromkeys(invalid))
    started = min(parsed)
    finished = max(parsed)
    return (
        started.isoformat(sep=" "),
        finished.isoformat(sep=" "),
        max(0.0, (finished - started).total_seconds()),
        tuple(dict.fromkeys(invalid)),
    )


def _parse_snapshot_time(value: object) -> datetime | None:
    normalized = normalize_market_datetime(value)
    if normalized is None:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


__all__ = ["MarketScanQueryMixin"]
