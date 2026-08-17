"""Minimal SQLite projections used by the trustworthy screening workbench."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import sqlite3
from typing import cast

from app.db.market_scan_integrity import verify_market_scan_snapshot
from app.models.market_scan import MarketScanResultItem, MarketScanResultStatus, MarketScanRun
from app.repositories.market_scan_context import MarketScanRepositoryContext
from app.repositories.market_scan_mapping import result_from_row, run_from_row
from app.repositories.market_scan_results import required_run_row


MAX_SCREENING_HYDRATION_SYMBOLS = 300
_DIMENSION_PREFIX = "$.score_details.components.score_dimensions.scores"


@dataclass(frozen=True, slots=True)
class MarketScanBreadthRow:
    """The only persisted values needed for breadth aggregation."""

    status: MarketScanResultStatus
    market: str
    score: int | None
    change_pct: float | None
    industry: str | None


@dataclass(frozen=True, slots=True)
class MarketScanScreeningRow:
    """Small executable-screen projection; large payload JSON remains in SQLite."""

    symbol: str
    code: str
    market: str
    name: str
    industry: str | None
    is_st: bool
    is_new: bool
    status: MarketScanResultStatus
    rank: int | None
    score: int | None
    raw_score: float | None
    trend_score: int | None
    data_quality_score: int | None
    change_pct: float | None
    turnover_rate: float | None
    amount: float | None
    alpha_5d: float | None
    confidence: float | None
    risk: float | None
    tradability: float | None


class MarketScanScreeningMixin(MarketScanRepositoryContext):
    def screening_breadth_snapshot(
        self,
        run_id: int,
    ) -> tuple[MarketScanRun, list[MarketScanBreadthRow]]:
        """Read a frozen run and its five-column breadth projection atomically."""

        with self._read_snapshot() as conn:
            run_row = required_run_row(conn, run_id)
            if str(run_row["status"]) in {"success", "degraded"}:
                verify_market_scan_snapshot(conn, run_id)
            rows = conn.execute(
                """
                SELECT status, market, score, change_pct, industry
                FROM market_scan_result
                WHERE run_id = ?
                ORDER BY symbol ASC
                """,
                (run_id,),
            ).fetchall()
        return run_from_row(run_row), [_breadth_row(row) for row in rows]

    def screening_evaluation_snapshot(
        self,
        run_id: int,
    ) -> tuple[MarketScanRun, list[MarketScanScreeningRow]]:
        """Read executable scalar fields without decoding full result payloads."""

        with self._read_snapshot() as conn:
            run_row = required_run_row(conn, run_id)
            if str(run_row["status"]) in {"success", "degraded"}:
                verify_market_scan_snapshot(conn, run_id)
            rows = conn.execute(_SCREENING_PROJECTION_SQL, (run_id,)).fetchall()
        return run_from_row(run_row), [_screening_row(row) for row in rows]

    def screening_result_items(
        self,
        run_id: int,
        symbols: Sequence[str],
    ) -> list[MarketScanResultItem]:
        """Hydrate only response rows after a terminal run has been validated."""

        unique_symbols = tuple(dict.fromkeys(symbols))
        if len(unique_symbols) > MAX_SCREENING_HYDRATION_SYMBOLS:
            raise ValueError("筛选结果单次最多读取 300 只股票详情")
        if not unique_symbols:
            return []
        placeholders = ",".join("?" for _symbol in unique_symbols)
        with self._read_snapshot() as conn:
            run_row = required_run_row(conn, run_id)
            if str(run_row["status"]) in {"success", "degraded"}:
                verify_market_scan_snapshot(conn, run_id)
            rows = conn.execute(
                f"""
                SELECT * FROM market_scan_result
                WHERE run_id = ? AND symbol IN ({placeholders})
                ORDER BY symbol ASC
                """,
                (run_id, *unique_symbols),
            ).fetchall()
        return [result_from_row(row) for row in rows]

def _numeric_dimension_sql(name: str) -> str:
    path = f"{_DIMENSION_PREFIX}.{name}"
    return (
        f"CASE WHEN json_type(metrics_json, '{path}') IN ('integer','real') "
        f"THEN json_extract(metrics_json, '{path}') END AS {name}"
    )


_SCREENING_PROJECTION_SQL = f"""
    SELECT symbol, code, market, name, industry, is_st, is_new, status,
           rank, score, raw_score, trend_score, data_quality_score,
           change_pct, turnover_rate, amount,
           {_numeric_dimension_sql("alpha_5d")},
           {_numeric_dimension_sql("confidence")},
           {_numeric_dimension_sql("risk")},
           {_numeric_dimension_sql("tradability")}
    FROM market_scan_result
    WHERE run_id = ?
    ORDER BY symbol ASC
"""


def _breadth_row(row: sqlite3.Row) -> MarketScanBreadthRow:
    return MarketScanBreadthRow(
        status=cast(MarketScanResultStatus, str(row["status"])),
        market=str(row["market"]),
        score=cast(int | None, row["score"]),
        change_pct=cast(float | None, row["change_pct"]),
        industry=cast(str | None, row["industry"]),
    )


def _screening_row(row: sqlite3.Row) -> MarketScanScreeningRow:
    return MarketScanScreeningRow(
        symbol=str(row["symbol"]),
        code=str(row["code"]),
        market=str(row["market"]),
        name=str(row["name"]),
        industry=cast(str | None, row["industry"]),
        is_st=bool(row["is_st"]),
        is_new=bool(row["is_new"]),
        status=cast(MarketScanResultStatus, str(row["status"])),
        rank=cast(int | None, row["rank"]),
        score=cast(int | None, row["score"]),
        raw_score=cast(float | None, row["raw_score"]),
        trend_score=cast(int | None, row["trend_score"]),
        data_quality_score=cast(int | None, row["data_quality_score"]),
        change_pct=cast(float | None, row["change_pct"]),
        turnover_rate=cast(float | None, row["turnover_rate"]),
        amount=cast(float | None, row["amount"]),
        alpha_5d=cast(float | None, row["alpha_5d"]),
        confidence=cast(float | None, row["confidence"]),
        risk=cast(float | None, row["risk"]),
        tradability=cast(float | None, row["tradability"]),
    )


__all__ = [
    "MAX_SCREENING_HYDRATION_SYMBOLS",
    "MarketScanBreadthRow",
    "MarketScanScreeningMixin",
    "MarketScanScreeningRow",
]
