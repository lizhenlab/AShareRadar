"""Persistence and compact execution reads for strategy evidence snapshots."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Mapping

from app.artifacts.io import ArtifactIOError, canonical_json_text, decode_json_bytes, sha256_hex
from app.db.market_scan_action_source import require_market_scan_action_source
from app.db.market_scan_integrity import MarketScanSnapshotSealError
from app.models.strategy_evidence import StrategyEvidenceCenter
from app.models.market_scan import MarketScanProductionScoreContract
from app.repositories.market_scan_score_diagnostics import read_production_score_contract
from app.models.strategy_execution import PortfolioCandidate, PortfolioDraftSummary
from app.repositories.base import SQLiteRepository
from app.utils.errors import NotFoundError


class StrategyEvidenceIntegrityError(RuntimeError):
    """Stored strategy evidence no longer matches its immutable seal."""


def strategy_evidence_digest(value: object) -> str:
    return sha256_hex(canonical_json_text(value))


class StrategyEvidenceRepository(SQLiteRepository):
    def __init__(self, path: Path, lock: threading.RLock | None = None) -> None:
        super().__init__(Path(path), lock or threading.RLock())

    def latest_execution(
        self,
        strategy_id: int,
        revision: int,
        mode: str,
    ) -> tuple[
        sqlite3.Row | None,
        PortfolioDraftSummary | None,
        list[PortfolioCandidate],
        MarketScanProductionScoreContract | None,
    ]:
        with self._lock, self._read_snapshot() as conn:
            row = conn.execute(
                """
                SELECT e.*,
                       r.snapshot_digest AS current_snapshot_digest,
                       r.snapshot_seal_origin AS current_snapshot_seal_origin,
                       r.success_count AS successful_result_count
                FROM strategy_execution AS e
                LEFT JOIN market_scan_run AS r ON r.id = e.market_scan_run_id
                WHERE e.strategy_id = ? AND e.strategy_revision = ?
                  AND (r.mode = ? OR r.id IS NULL)
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT 1
                """,
                (strategy_id, revision, mode),
            ).fetchone()
            if row is None:
                return None, None, [], None
            _verify_execution_source_snapshot(conn, row)
            score_contract = read_production_score_contract(
                conn,
                int(row["market_scan_run_id"]),
                expected_count=int(row["successful_result_count"] or 0),
            )
            candidate_rows = conn.execute(
                """
                    SELECT *
                    FROM strategy_execution_candidate
                    WHERE execution_id = ?
                    ORDER BY (utility_rank IS NULL) ASC, utility_rank ASC,
                             (original_rank IS NULL) ASC, original_rank ASC, symbol ASC
                    """,
                (int(row["id"]),),
            ).fetchall()
            summary = _validated_execution_summary(row)
            candidates = [_validated_candidate(item) for item in candidate_rows]
            _verify_execution_seal(row, summary, candidates)
        return row, summary, candidates, score_contract
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
        _verify_payload_binding(
            payload,
            strategy_id=strategy_id,
            revision=revision,
            fingerprint=fingerprint,
            mode=mode,
            status=status,
            generated_at=generated_at,
        )
        if strategy_evidence_digest(payload) != digest:
            raise StrategyEvidenceIntegrityError("策略证据快照摘要不一致")
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
        payload = _validated_stored_payload(row)
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


def _verify_execution_source_snapshot(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> None:
    if str(row["source_snapshot_verification_status"] or "") != "verified":
        raise StrategyEvidenceIntegrityError(
            "策略证据执行来源是未验证的旧版审计记录"
        )
    run_id = int(row["market_scan_run_id"])
    try:
        digest = require_market_scan_action_source(conn, run_id)
    except MarketScanSnapshotSealError as exc:
        raise StrategyEvidenceIntegrityError("策略证据执行来源榜单完整性校验失败") from exc
    if (
        str(row["source_snapshot_digest"] or "") != digest
        or str(row["source_snapshot_seal_origin"] or "") != "publication"
        or str(row["current_snapshot_digest"] or "") != digest
        or str(row["current_snapshot_seal_origin"] or "") != "publication"
    ):
        raise StrategyEvidenceIntegrityError("策略证据执行来源榜单绑定不一致")


def _validated_stored_payload(row: sqlite3.Row) -> dict[str, object]:
    try:
        payload = decode_json_bytes(str(row["evidence_json"]).encode("utf-8"))
    except ArtifactIOError as exc:
        raise StrategyEvidenceIntegrityError("策略证据快照 JSON 无效") from exc
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise StrategyEvidenceIntegrityError("策略证据快照根节点无效")
    if strategy_evidence_digest(payload) != str(row["evidence_digest"]):
        raise StrategyEvidenceIntegrityError("策略证据快照摘要不一致")
    _verify_payload_binding(
        payload,
        strategy_id=int(row["strategy_id"]),
        revision=int(row["strategy_revision"]),
        fingerprint=str(row["strategy_fingerprint"]),
        mode=str(row["mode"]),
        status=str(row["status"]),
        generated_at=str(row["generated_at"]),
    )
    return payload


def _verify_payload_binding(
    payload: Mapping[str, object],
    *,
    strategy_id: int,
    revision: int,
    fingerprint: str,
    mode: str,
    status: str,
    generated_at: str,
) -> None:
    expected = {
        "strategy_id": strategy_id,
        "strategy_version": revision,
        "strategy_fingerprint": fingerprint,
        "mode": mode,
        "status": status,
        "generated_at": generated_at,
    }
    if any(payload.get(name) != value for name, value in expected.items()):
        raise StrategyEvidenceIntegrityError("策略证据快照与数据库身份不一致")
    if "evidence_id" in payload or "evidence_digest" in payload:
        raise StrategyEvidenceIntegrityError("策略证据快照包含非封印字段")


def _validated_execution_summary(row: sqlite3.Row) -> PortfolioDraftSummary:
    try:
        summary = PortfolioDraftSummary.model_validate_json(str(row["summary_json"]), strict=True)
    except (TypeError, ValueError) as exc:
        raise StrategyEvidenceIntegrityError("策略执行摘要无效") from exc
    if summary.status != str(row["status"]):
        raise StrategyEvidenceIntegrityError("策略执行摘要状态与数据库不一致")
    return summary


def _validated_candidate(row: sqlite3.Row) -> PortfolioCandidate:
    try:
        candidate = PortfolioCandidate.model_validate_json(str(row["candidate_json"]), strict=True)
    except (TypeError, ValueError) as exc:
        raise StrategyEvidenceIntegrityError("策略执行候选无效") from exc
    expected = {
        "symbol": str(row["symbol"]),
        "original_rank": row["original_rank"],
        "utility_rank": row["utility_rank"],
        "status": str(row["status"]),
        "target_weight": float(row["target_weight"]),
        "pareto_front": bool(row["pareto_front"]),
    }
    if any(getattr(candidate, name) != value for name, value in expected.items()):
        raise StrategyEvidenceIntegrityError("策略执行候选与数据库索引列不一致")
    return candidate


def _verify_execution_seal(
    row: sqlite3.Row,
    summary: PortfolioDraftSummary,
    candidates: list[PortfolioCandidate],
) -> None:
    reconstructed = strategy_evidence_digest(
        {
            "execution_fingerprint": str(row["execution_fingerprint"]),
            "summary": summary.model_dump(mode="json"),
            "candidates": [item.model_dump(mode="json") for item in candidates],
        }
    )
    if reconstructed != str(row["result_digest"]):
        raise StrategyEvidenceIntegrityError("策略执行结果摘要不一致")


__all__ = [
    "StrategyEvidenceIntegrityError",
    "StrategyEvidenceRepository",
    "strategy_evidence_digest",
]
