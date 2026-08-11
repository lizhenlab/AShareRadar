"""Persistence and compact execution reads for strategy evidence snapshots."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from app.models.strategy_evidence import StrategyEvidenceCenter
from app.models.strategy_execution import PortfolioCandidate, PortfolioDraftSummary
from app.repositories.base import SQLiteRepository
from app.utils.errors import NotFoundError


class StrategyEvidenceRepository(SQLiteRepository):
    def __init__(self, path: Path, lock: threading.RLock | None = None) -> None:
        super().__init__(Path(path), lock or threading.RLock())

    def latest_execution(
        self,
        strategy_id: int,
        revision: int,
        mode: str,
    ) -> tuple[sqlite3.Row | None, PortfolioDraftSummary | None, list[PortfolioCandidate]]:
        with self._lock, self._read_snapshot() as conn:
            row = conn.execute(
                """
                SELECT e.*
                FROM strategy_execution AS e
                JOIN market_scan_run AS r ON r.id = e.market_scan_run_id
                WHERE e.strategy_id = ? AND e.strategy_revision = ? AND r.mode = ?
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT 1
                """,
                (strategy_id, revision, mode),
            ).fetchone()
            if row is None:
                return None, None, []
            candidates = [
                PortfolioCandidate.model_validate_json(str(item["candidate_json"]))
                for item in conn.execute(
                    """
                    SELECT candidate_json
                    FROM strategy_execution_candidate
                    WHERE execution_id = ?
                    ORDER BY (utility_rank IS NULL) ASC, utility_rank ASC,
                             (original_rank IS NULL) ASC, original_rank ASC, symbol ASC
                    """,
                    (int(row["id"]),),
                ).fetchall()
            ]
        return row, PortfolioDraftSummary.model_validate_json(str(row["summary_json"])), candidates

    def save(
        self,
        *,
        strategy_id: int,
        revision: int,
        fingerprint: str,
        mode: str,
        status: str,
        payload: dict[str, object],
        digest: str,
        generated_at: str,
    ) -> StrategyEvidenceCenter:
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO strategy_evidence_snapshot (
                    strategy_id, strategy_revision, strategy_fingerprint,
                    mode, status, evidence_json, evidence_digest, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    revision,
                    fingerprint,
                    mode,
                    status,
                    rendered,
                    digest,
                    generated_at,
                ),
            )
            evidence_id = cursor.lastrowid
        if evidence_id is None:
            raise RuntimeError("策略证据快照保存失败")
        return StrategyEvidenceCenter.model_validate(
            {
                **payload,
                "evidence_id": int(evidence_id),
                "evidence_digest": digest,
            }
        )

    def latest(
        self,
        strategy_id: int,
        *,
        revision: int | None,
        mode: str,
    ) -> StrategyEvidenceCenter | None:
        revision_clause = "AND strategy_revision = ?" if revision is not None else ""
        params: list[object] = [strategy_id, mode]
        if revision is not None:
            params.append(revision)
        with self._lock, self._read_snapshot() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM strategy_evidence_snapshot
                WHERE strategy_id = ? AND mode = ? {revision_clause}
                ORDER BY strategy_revision DESC, generated_at DESC, id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["evidence_json"]))
        return StrategyEvidenceCenter.model_validate(
            {
                **payload,
                "evidence_id": int(row["id"]),
                "evidence_digest": str(row["evidence_digest"]),
            }
        )

    def require_latest(
        self,
        strategy_id: int,
        *,
        revision: int | None,
        mode: str,
    ) -> StrategyEvidenceCenter:
        evidence = self.latest(strategy_id, revision=revision, mode=mode)
        if evidence is None:
            raise NotFoundError("策略证据中心尚未生成，请先显式刷新")
        return evidence


__all__ = ["StrategyEvidenceRepository"]
