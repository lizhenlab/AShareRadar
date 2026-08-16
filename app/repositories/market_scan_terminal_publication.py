"""Small helpers for validating and sealing terminal scan publication."""

from __future__ import annotations

import sqlite3

from app.db.market_scan_action_source import market_scan_diagnostics_authorize_action
from app.db.market_scan_integrity import seal_market_scan_snapshot
from app.models.market_scan import MarketScanPublicationDiagnostics, MarketScanRunStatus
from app.repositories.market_scan_action_gate_replay import (
    validate_current_action_gate_claim,
)
from app.repositories.market_scan_result_validation import (
    validate_persisted_production_skips,
)


def validated_publication_diagnostics(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    *,
    status: MarketScanRunStatus,
    diagnostics: MarketScanPublicationDiagnostics | None,
) -> MarketScanPublicationDiagnostics | None:
    if status not in {"success", "degraded"}:
        return diagnostics
    validate_persisted_production_skips(conn, run)
    if diagnostics is None or not market_scan_diagnostics_authorize_action(diagnostics):
        return diagnostics
    receipt = validate_current_action_gate_claim(conn, run, diagnostics)
    if receipt is None:
        return diagnostics
    return diagnostics.model_copy(
        update={"passed_gates": [*diagnostics.passed_gates, receipt]}
    )


def persist_terminal_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: MarketScanRunStatus,
    stamp: str,
    values: tuple[object, ...],
) -> None:
    conn.execute(
        """
        UPDATE market_scan_run
        SET status = ?, updated_at = ?, finished_at = ?, duration_ms = ?,
            current_stage = NULL, stage_started_at = NULL, stage_metrics_json = ?,
            message = ?, last_error = ?, publication_diagnostics_json = ?
        WHERE id = ?
        """,
        values,
    )
    if status in {"success", "degraded"}:
        seal_market_scan_snapshot(conn, run_id, sealed_at=stamp)


__all__ = ["persist_terminal_run", "validated_publication_diagnostics"]
