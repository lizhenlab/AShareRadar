from __future__ import annotations

import math
import sqlite3
from typing import Literal

from app.db.connection import SQLITE_AUDIT_EPOCH_FUNCTION
from app.models.market_scan import (
    MARKET_SCAN_TOP100_REFRESH_SCOPE,
    MarketScanCoverage,
    MarketScanFilterValues,
    MarketScanMode,
    MarketScanPublicationSummary,
    MarketScanResultItem,
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanRunStatus,
    MarketScanSortOrderValues,
    MarketScanSortValues,
    MarketScanStaleCluster,
)
from app.repositories.market_scan_context import MarketScanRepositoryContext
from app.repositories.market_scan_filtering import market_scan_result_filter_sql
from app.repositories.market_scan_lifecycle import ACTIVE_SCAN_STATUSES
from app.repositories.market_scan_mapping import append_exact_filter, page_count, result_from_row, result_order_sql, run_from_row
from app.repositories.market_scan_publication_summary import publication_summary_from_evidence
from app.repositories.market_scan_results import count_degraded_results, required_run_row


class MarketScanQueryMixin(MarketScanRepositoryContext):
    def run(self, run_id: int) -> MarketScanRun:
        with self._read_snapshot() as conn:
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

    def latest_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None:
        where = "WHERE mode = ?" if mode is not None else ""
        parameters: tuple[object, ...] = (mode,) if mode is not None else ()
        with self._read_snapshot() as conn:
            row = conn.execute(
                f"SELECT * FROM market_scan_run {where} ORDER BY id DESC LIMIT 1",
                parameters,
            ).fetchone()
        return run_from_row(row) if row is not None else None

    def latest_full_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None:
        clauses = ["scope != ?"]
        parameters: list[object] = [MARKET_SCAN_TOP100_REFRESH_SCOPE]
        if mode is not None:
            clauses.append("mode = ?")
            parameters.append(mode)
        with self._read_snapshot() as conn:
            row = conn.execute(
                f"SELECT * FROM market_scan_run WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 1",
                parameters,
            ).fetchone()
        return run_from_row(row) if row is not None else None

    def latest_published_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None:
        clauses = ["status IN ('success', 'degraded')"]
        parameters: list[object] = []
        if mode is not None:
            clauses.append("mode = ?")
            parameters.append(mode)
        with self._read_snapshot() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM market_scan_run
                WHERE {' AND '.join(clauses)}
                ORDER BY data_date DESC,
                         {SQLITE_AUDIT_EPOCH_FUNCTION}(finished_at) DESC,
                         id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return run_from_row(row) if row is not None else None

    def list_runs(
        self,
        *,
        page: int,
        page_size: int,
        mode: MarketScanMode | None = None,
        status: MarketScanRunStatus | Literal["published"] | None = None,
        data_date: str | None = None,
    ) -> MarketScanRunPage:
        clauses: list[str] = []
        parameters: list[object] = []
        append_exact_filter(clauses, parameters, "mode", mode)
        if status == "published":
            clauses.append("status IN ('success', 'degraded')")
        else:
            append_exact_filter(clauses, parameters, "status", status)
        append_exact_filter(clauses, parameters, "data_date", data_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        offset = (page - 1) * page_size
        with self._read_snapshot() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM market_scan_run {where}",
                    parameters,
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT * FROM market_scan_run
                {where}
                ORDER BY {SQLITE_AUDIT_EPOCH_FUNCTION}(created_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, page_size, offset),
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

    def success_raw_scores(self, run_id: int) -> tuple[object, ...]:
        """Return the complete success-score multiset without hydrating result rows."""
        with self._read_snapshot() as conn:
            rows = conn.execute(
                """
                SELECT raw_score
                FROM market_scan_result
                WHERE run_id = ? AND status = 'success'
                ORDER BY symbol ASC
                """,
                (run_id,),
            ).fetchall()
        return tuple(row["raw_score"] for row in rows)

    def publication_summary(self, run_id: int) -> MarketScanPublicationSummary:
        with self._lock, self._connect() as conn:
            run = required_run_row(conn, run_id)
            coverage_rows = _publication_coverage_rows(conn, run_id)
            stale_rows = _publication_stale_rows(conn, run_id, data_date=run["data_date"])
            timestamp_rows = _publication_timestamp_rows(conn, run_id)
        total_count = int(run["total_count"] or 0)
        return publication_summary_from_evidence(
            run,
            _coverage_summary(coverage_rows),
            _systemic_stale_cluster(stale_rows, total_count=total_count),
            timestamp_rows,
        )

    def results_page(
        self,
        run_id: int,
        *,
        page: int,
        page_size: int,
        status: MarketScanResultStatus | None,
        market: MarketScanFilterValues,
        industry: MarketScanFilterValues,
        is_st: bool | None,
        is_new: bool | None,
        min_score: int | None = None, max_score: int | None = None,
        min_trend_score: int | None = None, max_trend_score: int | None = None,
        min_change_pct: float | None = None, max_change_pct: float | None = None,
        min_turnover_rate: float | None = None, max_turnover_rate: float | None = None,
        min_amount: float | None = None, max_amount: float | None = None,
        min_data_quality_score: int | None, max_data_quality_score: int | None = None,
        min_confidence: float | None = None, max_risk: float | None = None,
        min_tradability: float | None = None, keyword: str | None,
        symbols: MarketScanFilterValues = None, sort: MarketScanSortValues,
        order: MarketScanSortOrderValues,
    ) -> MarketScanResultPage:
        where, params = market_scan_result_filter_sql(
            run_id,
            status=status,
            market=market,
            industry=industry,
            is_st=is_st,
            is_new=is_new,
            min_score=min_score, max_score=max_score,
            min_trend_score=min_trend_score, max_trend_score=max_trend_score,
            min_change_pct=min_change_pct, max_change_pct=max_change_pct,
            min_turnover_rate=min_turnover_rate, max_turnover_rate=max_turnover_rate,
            min_amount=min_amount, max_amount=max_amount,
            min_data_quality_score=min_data_quality_score, max_data_quality_score=max_data_quality_score,
            min_confidence=min_confidence, max_risk=max_risk, min_tradability=min_tradability,
            keyword=keyword, symbols=symbols,
        )
        order_sql = result_order_sql(sort, order)
        offset = (page - 1) * page_size
        with self._read_snapshot() as conn:
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


def _publication_coverage_rows(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT market,
               SUM(CASE WHEN status IN ('success', 'missing') THEN 1 ELSE 0 END) AS total_count,
               SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
               SUM(CASE WHEN status = 'missing' THEN 1 ELSE 0 END) AS missing_count,
               SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count
        FROM market_scan_result
        WHERE run_id = ?
        GROUP BY market
        """,
        (run_id,),
    ).fetchall()


def _publication_stale_rows(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    data_date: object,
) -> list[sqlite3.Row]:
    return conn.execute(
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
        (run_id, data_date),
    ).fetchall()


def _publication_timestamp_rows(
    conn: sqlite3.Connection,
    run_id: int,
) -> list[tuple[str, str | None, str | None]]:
    rows = conn.execute(
        """
        SELECT market, quote_timestamp, quote_observed_at
        FROM market_scan_result
        WHERE run_id = ? AND status != 'pending'
        ORDER BY market ASC, symbol ASC
        """,
        (run_id,),
    ).fetchall()
    return [
        (
            str(row["market"]),
            str(row["quote_timestamp"]) if row["quote_timestamp"] is not None else None,
            str(row["quote_observed_at"]) if row["quote_observed_at"] is not None else None,
        )
        for row in rows
    ]


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


__all__ = ["MarketScanQueryMixin"]
