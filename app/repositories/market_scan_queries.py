from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3
from typing import Literal

from app.db.connection import SQLITE_AUDIT_EPOCH_FUNCTION
from app.db.market_scan_integrity import verify_market_scan_snapshot
from app.models.market_scan import (
    MARKET_SCAN_FULL_MARKET_SCOPE,
    MarketScanAutomaticState,
    MarketScanFilterValues,
    MarketScanMode,
    MarketScanPublicationSummary,
    MarketScanProductionScoreContract,
    MarketScanResultItem,
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanRunStatus,
    MarketScanSortOrderValues,
    MarketScanSortValues,
    MarketScanScoreDistributionObservation,
)
from app.models.market_scan_polling import MarketScanPollingIdentity
from app.repositories.market_scan_context import MarketScanRepositoryContext
from app.repositories.market_scan_automatic_state import market_scan_automatic_state_from_row
from app.repositories.market_scan_lifecycle import ACTIVE_SCAN_STATUSES
from app.repositories.market_scan_mapping import (
    append_exact_filter,
    page_count,
    result_from_row,
    run_from_row,
)
from app.repositories.market_scan_polling_identity import read_market_scan_polling_identity
from app.repositories.market_scan_results import count_degraded_results, required_run_row
from app.repositories.market_scan_score_diagnostics import (
    read_production_score_contract,
    read_publication_summary,
    read_success_score_observations,
)
from app.repositories.market_scan_verified_read import (
    MARKET_SCAN_VERIFIED_RESULT_READ_ARGUMENTS,
    VerifiedMarketScanRead,
    verified_market_scan_read,
)

class MarketScanQueryMixin(MarketScanRepositoryContext):
    def run(self, run_id: int) -> MarketScanRun:
        with self._read_snapshot() as conn:
            row = required_run_row(conn, run_id)
            _verify_published_row(conn, row)
        return run_from_row(row)

    @contextmanager
    def verified_read(self, run_id: int) -> Iterator[VerifiedMarketScanRead]:
        """Open one non-reusable request snapshot with exactly one full verification."""
        with verified_market_scan_read(self._path, run_id) as session:
            yield session

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
            if row is not None:
                _verify_published_row(conn, row)
        return run_from_row(row) if row is not None else None

    def polling_identity(self, *, mode: MarketScanMode) -> MarketScanPollingIdentity:
        """Return change tokens only; this result has no publication authority."""
        return read_market_scan_polling_identity(
            database_path=self._path,
            read_snapshot=self._read_snapshot,
            mode=mode,
        )

    def latest_full_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None:
        clauses = ["scope = ?"]
        parameters: list[object] = [MARKET_SCAN_FULL_MARKET_SCOPE]
        if mode is not None:
            clauses.append("mode = ?")
            parameters.append(mode)
        with self._read_snapshot() as conn:
            row = conn.execute(
                f"SELECT * FROM market_scan_run WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 1",
                parameters,
            ).fetchone()
            if row is not None:
                _verify_published_row(conn, row)
        return run_from_row(row) if row is not None else None

    def latest_full_automatic_state(self) -> MarketScanAutomaticState | None:
        """Read a lightweight identity; this does not authorize use of a publication."""
        with self._read_snapshot() as conn:
            schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
            row = conn.execute(
                """
                SELECT id, status, trigger, data_date, scope, mode, rule_version,
                       updated_at, snapshot_digest, snapshot_seal_origin,
                       snapshot_sealed_at, finished_at
                FROM market_scan_run
                WHERE scope = ? AND mode = 'official'
                ORDER BY id DESC
                LIMIT 1
                """,
                (MARKET_SCAN_FULL_MARKET_SCOPE,),
            ).fetchone()
        if row is None:
            return None
        return market_scan_automatic_state_from_row(
            row,
            database_path=self._path,
            schema_version=schema_version,
        )

    def latest_published_run(self, *, mode: MarketScanMode | None = None) -> MarketScanRun | None:
        clauses = [
            "status IN ('success', 'degraded')",
            "scope = ?",
        ]
        parameters: list[object] = [MARKET_SCAN_FULL_MARKET_SCOPE]
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
            if row is not None:
                _verify_published_row(conn, row)
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
        return self._list_runs(
            page=page,
            page_size=page_size,
            mode=mode,
            status=status,
            data_date=data_date,
            verify_published=True,
        )

    def list_run_identities(
        self,
        *,
        page: int,
        page_size: int,
        mode: MarketScanMode | None = None,
        status: MarketScanRunStatus | Literal["published"] | None = None,
        data_date: str | None = None,
    ) -> MarketScanRunPage:
        """Return navigation-only rows that cannot authorize a publication read."""
        return self._list_runs(
            page=page,
            page_size=page_size,
            mode=mode,
            status=status,
            data_date=data_date,
            verify_published=False,
        )

    def _list_runs(
        self,
        *,
        page: int,
        page_size: int,
        mode: MarketScanMode | None,
        status: MarketScanRunStatus | Literal["published"] | None,
        data_date: str | None,
        verify_published: bool,
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
            if verify_published:
                for row in rows:
                    _verify_published_row(conn, row)
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

    def success_score_observations(
        self,
        run_id: int,
    ) -> tuple[MarketScanScoreDistributionObservation, ...]:
        """Return auditable base/integer/final layers without hydrating result models."""
        with self._read_snapshot() as conn:
            return read_success_score_observations(conn, run_id)

    def success_score_contract(
        self,
        run_id: int,
    ) -> MarketScanProductionScoreContract | None:
        """Return one fully covered, internally consistent production score contract."""
        with self._read_snapshot() as conn:
            run = required_run_row(conn, run_id)
            return read_production_score_contract(
                conn,
                run_id,
                expected_count=int(run["success_count"] or 0),
            )

    def publication_summary(self, run_id: int) -> MarketScanPublicationSummary:
        with self._read_snapshot() as conn:
            run = required_run_row(conn, run_id)
            if str(run["status"]) in {"success", "degraded"}:
                verify_market_scan_snapshot(conn, run_id)
            return read_publication_summary(conn, run)

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
        values = locals()
        with self.verified_read(run_id) as verified:
            return verified.results_page(
                **{
                    name: values[name]
                    for name in MARKET_SCAN_VERIFIED_RESULT_READ_ARGUMENTS
                }
            )


def _verify_published_row(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    if str(row["status"]) in {"success", "degraded"}:
        verify_market_scan_snapshot(conn, int(row["id"]))


__all__ = ["MarketScanQueryMixin"]
