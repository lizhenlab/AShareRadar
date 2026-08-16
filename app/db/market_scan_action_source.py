"""Fail-closed eligibility checks for actions sourced from market scans."""

from __future__ import annotations

from collections.abc import Mapping
import re
import sqlite3
from dataclasses import dataclass

from app.artifacts.io import ArtifactIOError, decode_json_bytes
from app.db.market_scan_integrity import (
    MarketScanSnapshotSealError,
    require_publication_market_scan_snapshot,
)
from app.models.market_scan import (
    MARKET_SCAN_FULL_MARKET_SCOPE,
    MarketScanPublicationDiagnostic,
    MarketScanPublicationDiagnostics,
    MarketScanScoreDistributionObservation,
)
from app.repositories.market_scan_result_validation import (
    validate_persisted_production_skips,
)
from app.repositories.market_scan_action_gate_replay import (
    MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE,
    MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_PREFIX,
    replay_current_action_gate_receipt_from_verified_observations,
)
from app.repositories.market_scan_score_diagnostics import (
    score_observation_from_canonical_result,
)


MARKET_SCAN_ACTION_SCORE_GATE = "score_distribution.pass"


class MarketScanActionSourceError(MarketScanSnapshotSealError):
    """A sealed scan is readable for audit but cannot authorize an action."""


@dataclass(frozen=True)
class MarketScanActionSourceInspection:
    """One fully verified snapshot read and its downstream action eligibility."""

    snapshot_digest: str
    eligible: bool
    reason: str | None = None


class _ScoreObservationCollector:
    def __init__(self) -> None:
        self.observations: list[MarketScanScoreDistributionObservation] = []
        self.invalid = False

    def __call__(self, row: Mapping[str, object]) -> None:
        try:
            observation = score_observation_from_canonical_result(row)
        except ValueError:
            self.invalid = True
            return
        if observation is not None:
            self.observations.append(observation)


def require_market_scan_action_source(
    conn: sqlite3.Connection,
    run_id: int,
) -> str:
    """Require an original, full-market snapshot with a passed score gate."""

    try:
        inspection = inspect_market_scan_action_source(conn, run_id)
    except MarketScanSnapshotSealError as exc:
        raise MarketScanActionSourceError(
            f"扫描批次 {run_id} 不能证明原发布动作来源"
        ) from exc
    if not inspection.eligible:
        raise MarketScanActionSourceError(
            inspection.reason or f"扫描批次 {run_id} 不能授权动作"
        )
    return inspection.snapshot_digest


def inspect_market_scan_action_source(
    conn: sqlite3.Connection,
    run_id: int,
) -> MarketScanActionSourceInspection:
    """Verify one snapshot once and report post-verification action eligibility."""

    score_observations = _ScoreObservationCollector()
    digest = require_publication_market_scan_snapshot(
        conn,
        run_id,
        result_observer=score_observations,
    )
    try:
        _require_action_eligibility(conn, run_id, score_observations)
    except MarketScanActionSourceError as exc:
        return MarketScanActionSourceInspection(
            snapshot_digest=digest,
            eligible=False,
            reason=str(exc),
        )
    return MarketScanActionSourceInspection(snapshot_digest=digest, eligible=True)


def _require_action_eligibility(
    conn: sqlite3.Connection,
    run_id: int,
    score_observations: _ScoreObservationCollector,
) -> None:
    row = conn.execute(
        """
        SELECT *
        FROM market_scan_run
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise MarketScanActionSourceError(f"动作来源扫描批次不存在：{run_id}")
    if str(row["scope"]) != MARKET_SCAN_FULL_MARKET_SCOPE:
        raise MarketScanActionSourceError(
            f"动作来源必须是完整全市场扫描批次：{run_id}"
        )
    diagnostics = _action_diagnostics(row["publication_diagnostics_json"], run_id)
    if not market_scan_diagnostics_authorize_action(diagnostics):
        raise MarketScanActionSourceError(
            f"扫描批次 {run_id} 未通过动作所需的评分分布门禁"
    )
    _require_current_canonical_replay_receipt(
        conn,
        row,
        diagnostics,
        score_observations,
    )
    try:
        validate_persisted_production_skips(conn, row)
    except ValueError as exc:
        raise MarketScanActionSourceError(
            f"扫描批次 {run_id} 包含未证实的跳过样本"
        ) from exc


def market_scan_diagnostics_authorize_action(
    diagnostics: MarketScanPublicationDiagnostics,
) -> bool:
    passed = [
        item
        for item in diagnostics.passed_gates
        if item.code == MARKET_SCAN_ACTION_SCORE_GATE and item.severity == "info"
    ]
    conflicting = any(
        item.code.startswith("score_distribution.")
        for item in (*diagnostics.blockers, *diagnostics.source_warnings)
    )
    unexpected_score_gate = any(
        item.code.startswith("score_distribution.")
        and item.code != MARKET_SCAN_ACTION_SCORE_GATE
        for item in diagnostics.passed_gates
    )
    return (
        not diagnostics.blockers
        and len(passed) == 1
        and not conflicting
        and not unexpected_score_gate
    )


def _action_diagnostics(
    value: object,
    run_id: int,
) -> MarketScanPublicationDiagnostics:
    if not isinstance(value, str) or not value.strip():
        raise MarketScanActionSourceError(
            f"扫描批次 {run_id} 缺少动作所需的发布诊断证据"
        )
    try:
        decoded = decode_json_bytes(value.encode("utf-8"))
        return MarketScanPublicationDiagnostics.model_validate(decoded)
    except (ArtifactIOError, TypeError, ValueError) as exc:
        raise MarketScanActionSourceError(
            f"扫描批次 {run_id} 的发布诊断证据无效"
        ) from exc


def _require_current_canonical_replay_receipt(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    diagnostics: MarketScanPublicationDiagnostics,
    score_observations: _ScoreObservationCollector,
) -> None:
    if not _is_current_scan_run(run):
        return
    if score_observations.invalid:
        raise MarketScanActionSourceError(
            f"扫描批次 {run['id']} 包含无法重放的评分观测"
        )
    _require_current_score_registry(conn, run)
    receipt, without_receipt = _stored_canonical_replay_receipt(run, diagnostics)
    _require_exact_replayed_receipt(
        conn,
        run,
        without_receipt,
        receipt,
        observations=tuple(score_observations.observations),
    )


def _is_current_scan_run(run: sqlite3.Row) -> bool:
    return re.fullmatch(
        r"full-market-scan-v6:[0-9a-f]{64}",
        str(run["rule_version"] or ""),
    ) is not None


def _require_current_score_registry(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
) -> None:
    registered = conn.execute(
        """
        SELECT production_score_rule_version
        FROM market_scan_rule_contract
        WHERE rule_version = ?
        """,
        (run["rule_version"],),
    ).fetchone()
    if registered is None:
        raise MarketScanActionSourceError(
            f"扫描批次 {run['id']} 缺少当前规则合同注册"
        )
    if str(registered["production_score_rule_version"] or "") != "full-market-score-v5":
        raise MarketScanActionSourceError(
            f"扫描批次 {run['id']} 不是当前可写评分合同"
        )


def _stored_canonical_replay_receipt(
    run: sqlite3.Row,
    diagnostics: MarketScanPublicationDiagnostics,
) -> tuple[MarketScanPublicationDiagnostic, MarketScanPublicationDiagnostics]:
    receipts = [
        item
        for item in diagnostics.passed_gates
        if item.code == MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE
        and item.severity == "info"
    ]
    pattern = re.compile(
        re.escape(MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_PREFIX) + r"[0-9a-f]{64}"
    )
    if len(receipts) != 1 or pattern.fullmatch(receipts[0].detail) is None:
        raise MarketScanActionSourceError(
            f"扫描批次 {run['id']} 缺少当前发布规范重放回执"
        )
    misplaced = any(
        item.code == MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE
        for item in (*diagnostics.blockers, *diagnostics.source_warnings)
    )
    if misplaced:
        raise MarketScanActionSourceError(
            f"扫描批次 {run['id']} 发布规范重放回执位置无效"
        )
    without_receipt = diagnostics.model_copy(
        update={
            "passed_gates": [
                item
                for item in diagnostics.passed_gates
                if item.code != MARKET_SCAN_CANONICAL_REPLAY_RECEIPT_CODE
            ]
        }
    )
    return receipts[0], without_receipt


def _require_exact_replayed_receipt(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    diagnostics_without_receipt: MarketScanPublicationDiagnostics,
    receipt: MarketScanPublicationDiagnostic,
    *,
    observations: tuple[MarketScanScoreDistributionObservation, ...],
) -> None:
    try:
        expected = replay_current_action_gate_receipt_from_verified_observations(
            conn,
            run,
            diagnostics_without_receipt,
            observations,
        )
    except ValueError as exc:
        raise MarketScanActionSourceError(
            f"扫描批次 {run['id']} 发布规范重放回执无法由持久化证据重放"
        ) from exc
    if expected is None or expected != receipt:
        raise MarketScanActionSourceError(
            f"扫描批次 {run['id']} 发布规范重放回执与持久化证据不一致"
        )


__all__ = [
    "MARKET_SCAN_ACTION_SCORE_GATE",
    "MarketScanActionSourceInspection",
    "MarketScanActionSourceError",
    "inspect_market_scan_action_source",
    "market_scan_diagnostics_authorize_action",
    "require_market_scan_action_source",
]
