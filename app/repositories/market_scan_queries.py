from __future__ import annotations

from app.models.market_scan import (
    MarketScanResultItem,
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanSort,
    MarketScanSortOrder,
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
                """
                SELECT * FROM market_scan_run
                ORDER BY created_at DESC, id DESC
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


__all__ = ["MarketScanQueryMixin"]
