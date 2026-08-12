"""Persistence and frozen-scan reads for StrategySpec executions."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import threading
from pathlib import Path

from app.models.market_scan import (
    MARKET_SCAN_TOP100_REFRESH_SCOPE,
    MarketScanResultItem,
    MarketScanRun,
)
from app.models.strategy_execution import (
    PortfolioCandidate,
    PortfolioCandidatePage,
    PortfolioCandidateStatus,
    PortfolioCandidateSort,
    PortfolioDraft,
    PortfolioDraftSummary,
    StrategyExecutionContext,
    StrategyExecutionPage,
    StrategyExecutionMarketScanMode,
)
from app.repositories.base import SQLiteRepository
from app.repositories.market_scan_mapping import result_from_row, run_from_row
from app.utils.errors import NotFoundError


@dataclass(frozen=True)
class FrozenMarketScan:
    run: MarketScanRun
    items: list[MarketScanResultItem]


class StrategyExecutionRepository(SQLiteRepository):
    def __init__(self, path: Path, lock: threading.RLock | None = None) -> None:
        super().__init__(Path(path), lock or threading.RLock())

    def frozen_scan(
        self,
        *,
        run_id: int | None,
        data_date: str | None,
        mode: StrategyExecutionMarketScanMode,
    ) -> FrozenMarketScan:
        with self._lock, self._read_snapshot() as conn:
            run_row = _published_run_row(
                conn,
                run_id=run_id,
                data_date=data_date,
                mode=mode,
            )
            run = run_from_row(run_row)
            rows = conn.execute(
                """
                SELECT * FROM market_scan_result
                WHERE run_id = ?
                ORDER BY (rank IS NULL) ASC, rank ASC, symbol ASC
                """,
                (run.id,),
            ).fetchall()
            items = [result_from_row(row) for row in rows]
        return FrozenMarketScan(run=run, items=items)

    def save(
        self,
        *,
        strategy_id: int,
        strategy_revision: int,
        strategy_fingerprint: str,
        execution_fingerprint: str,
        kind: str,
        run: MarketScanRun,
        cost_rule_fingerprint: str,
        status: str,
        summary: PortfolioDraftSummary,
        candidates: list[PortfolioCandidate],
        result_digest: str,
        timestamp: str,
    ) -> int:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT INTO strategy_execution (
                    strategy_id, strategy_revision, strategy_fingerprint,
                    execution_fingerprint, kind, market_scan_run_id,
                    rule_version, data_as_of, data_date, cost_rule_fingerprint,
                    status, summary_json, result_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    strategy_revision,
                    strategy_fingerprint,
                    execution_fingerprint,
                    kind,
                    run.id,
                    run.rule_version,
                    run.as_of,
                    run.data_date,
                    cost_rule_fingerprint,
                    status,
                    summary.model_dump_json(),
                    result_digest,
                    timestamp,
                ),
            )
            execution_id = cursor.lastrowid
            if execution_id is None:
                raise RuntimeError("策略执行保存失败")
            _insert_candidates(conn, int(execution_id), candidates)
        return int(execution_id)

    def draft(self, execution_id: int) -> PortfolioDraft:
        with self._lock, self._read_snapshot() as conn:
            row = _execution_row(conn, execution_id)
            context = _context_from_row(row)
            summary = PortfolioDraftSummary.model_validate_json(str(row["summary_json"]))
            selected_rows = conn.execute(
                """
                SELECT candidate_json FROM strategy_execution_candidate
                WHERE execution_id = ?
                  AND status IN ('selected', 'constraint_adjusted')
                ORDER BY utility_rank ASC, original_rank ASC, symbol ASC
                """,
                (execution_id,),
            ).fetchall()
            preview_rows = conn.execute(
                """
                SELECT candidate_json FROM strategy_execution_candidate
                WHERE execution_id = ?
                ORDER BY (utility_rank IS NULL) ASC, utility_rank ASC,
                         (original_rank IS NULL) ASC, original_rank ASC, symbol ASC
                LIMIT 100
                """,
                (execution_id,),
            ).fetchall()
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM strategy_execution_candidate WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()[0]
            )
        return PortfolioDraft(
            context=context,
            summary=summary,
            selected=[_candidate_from_row(item) for item in selected_rows],
            candidate_preview=[_candidate_from_row(item) for item in preview_rows],
            candidate_total=total,
            result_digest=str(row["result_digest"]),
        )

    def candidates(
        self,
        execution_id: int,
        *,
        page: int,
        page_size: int,
        status: PortfolioCandidateStatus | None,
        sort_by: PortfolioCandidateSort = "utility_score",
        descending: bool = True,
    ) -> PortfolioCandidatePage:
        offset = (page - 1) * page_size
        where = "execution_id = ?"
        params: list[object] = [execution_id]
        if status is not None:
            where += " AND status = ?"
            params.append(status)
        with self._lock, self._read_snapshot() as conn:
            _execution_row(conn, execution_id)
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM strategy_execution_candidate WHERE {where}",
                    params,
                ).fetchone()[0]
            )
            order_expression = _CANDIDATE_SORT_SQL[sort_by]
            direction = "DESC" if descending else "ASC"
            rows = conn.execute(
                f"""
                SELECT candidate_json FROM strategy_execution_candidate
                WHERE {where}
                ORDER BY ({order_expression} IS NULL) ASC, {order_expression} {direction},
                         utility_rank ASC, original_rank ASC, symbol ASC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
        return PortfolioCandidatePage(
            execution_id=execution_id,
            items=[_candidate_from_row(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
            page_count=(total + page_size - 1) // page_size,
        )

    def executions(
        self,
        *,
        strategy_id: int,
        page: int,
        page_size: int,
    ) -> StrategyExecutionPage:
        offset = (page - 1) * page_size
        with self._lock, self._read_snapshot() as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM strategy_execution WHERE strategy_id = ?",
                    (strategy_id,),
                ).fetchone()[0]
            )
            rows = conn.execute(
                """
                SELECT * FROM strategy_execution
                WHERE strategy_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (strategy_id, page_size, offset),
            ).fetchall()
        return StrategyExecutionPage(
            items=[_context_from_row(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
            page_count=(total + page_size - 1) // page_size,
        )


def _published_run_row(
    conn: sqlite3.Connection,
    *,
    run_id: int | None,
    data_date: str | None,
    mode: StrategyExecutionMarketScanMode,
) -> sqlite3.Row:
    if mode not in {"official", "intraday"}:
        raise ValueError("策略执行仅接受盘后正式或盘中临时扫描，不接受盘前复盘批次")
    if run_id is not None:
        row = conn.execute(
            "SELECT * FROM market_scan_run WHERE id = ?",
            (run_id,),
        ).fetchone()
    elif data_date is not None:
        row = conn.execute(
            """
            SELECT * FROM market_scan_run
            WHERE data_date = ? AND mode = ? AND status IN ('success', 'degraded')
              AND scope != ?
            ORDER BY as_of DESC, id DESC
            LIMIT 1
            """,
            (data_date, mode, MARKET_SCAN_TOP100_REFRESH_SCOPE),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT * FROM market_scan_run
            WHERE mode = ? AND status IN ('success', 'degraded')
              AND scope != ?
            ORDER BY data_date DESC, as_of DESC, id DESC
            LIMIT 1
            """,
            (mode, MARKET_SCAN_TOP100_REFRESH_SCOPE),
        ).fetchone()
    if row is None:
        target = f"批次 {run_id}" if run_id is not None else f"日期 {data_date or '最新'}"
        raise NotFoundError(f"找不到可重放的冻结全市场扫描：{target}")
    if str(row["status"]) not in {"success", "degraded"}:
        raise ValueError(f"全市场扫描尚未发布，不可作为策略证据：{int(row['id'])}")
    if run_id is not None and str(row["mode"]) != mode:
        raise ValueError(f"扫描模式不匹配：批次为 {row['mode']}，请求为 {mode}")
    return row


def _execution_row(conn: sqlite3.Connection, execution_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM strategy_execution WHERE id = ?",
        (execution_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"策略执行不存在：{execution_id}")
    return row


def _context_from_row(row: sqlite3.Row) -> StrategyExecutionContext:
    return StrategyExecutionContext(
        execution_id=int(row["id"]),
        strategy_id=int(row["strategy_id"]),
        strategy_version=int(row["strategy_revision"]),
        strategy_fingerprint=str(row["strategy_fingerprint"]),
        execution_fingerprint=str(row["execution_fingerprint"]),
        kind=str(row["kind"]),
        market_scan_run_id=int(row["market_scan_run_id"]),
        rule_version=str(row["rule_version"]),
        data_as_of=str(row["data_as_of"]),
        data_date=str(row["data_date"]),
        cost_rule_fingerprint=str(row["cost_rule_fingerprint"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
    )


def _candidate_from_row(row: sqlite3.Row) -> PortfolioCandidate:
    return PortfolioCandidate.model_validate_json(str(row["candidate_json"]))


def _insert_candidates(
    conn: sqlite3.Connection,
    execution_id: int,
    candidates: list[PortfolioCandidate],
) -> None:
    conn.executemany(
        """
        INSERT INTO strategy_execution_candidate (
            execution_id, symbol, original_rank, utility_rank, status,
            target_weight, pareto_front, candidate_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                execution_id,
                item.symbol,
                item.original_rank,
                item.utility_rank,
                item.status,
                item.target_weight,
                int(item.pareto_front),
                item.model_dump_json(),
            )
            for item in candidates
        ],
    )


_CANDIDATE_SORT_SQL: dict[PortfolioCandidateSort, str] = {
    "utility_score": "json_extract(candidate_json, '$.utility_score')",
    "alpha_1d": "json_extract(candidate_json, '$.alpha_1d')",
    "alpha_5d": "json_extract(candidate_json, '$.alpha_5d')",
    "alpha_20d": "json_extract(candidate_json, '$.alpha_20d')",
    "confidence": "json_extract(candidate_json, '$.confidence')",
    "risk": "json_extract(candidate_json, '$.risk')",
    "tradability": "json_extract(candidate_json, '$.tradability')",
    "original_rank": "original_rank",
}


__all__ = ["FrozenMarketScan", "StrategyExecutionRepository"]
