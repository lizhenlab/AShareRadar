"""Read-only inventories of frozen research inputs; never estimate performance."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3

from app.db.market_scan_integrity import MarketScanSnapshotSealError, verify_market_scan_snapshot
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE
from app.repositories.market_scan_mapping import decode_result_payload, result_from_row, run_from_row
from app.services.market_scan_score_dimensions import verify_market_scan_point_in_time_evidence
from app.services.trading_calendar import TradingCalendarCoverageError, next_trade_dates


RESEARCH_INPUT_MANIFEST_VERSION = "market-scan-research-input-manifest-v1"


@dataclass(frozen=True)
class ResearchInputConfig:
    """Selection is frozen before inspection, without consulting any return."""

    as_of_date: str
    horizon: int = 5
    run_ids: tuple[int, ...] = ()
    max_runs: int = 3

    def __post_init__(self) -> None:
        if date.fromisoformat(self.as_of_date).isoformat() != self.as_of_date:
            raise ValueError("as_of_date must be an ISO date")
        if self.horizon < 1 or self.max_runs < 1 or any(run_id < 1 for run_id in self.run_ids):
            raise ValueError("horizon, max_runs and run_ids must be positive")
        if len(set(self.run_ids)) != len(self.run_ids):
            raise ValueError("run_ids must be unique")


@contextmanager
def readonly_research_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """A consistent read transaction; no migrations, repairs, seals or indices."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def audit_research_inputs(database: str | Path, config: ResearchInputConfig) -> dict[str, object]:
    """Return a digest-bound manifest, including explicit inaccessible-data states."""
    report: dict[str, object] = {
        "contract_version": RESEARCH_INPUT_MANIFEST_VERSION,
        "selection": asdict(config),
        "selection_policy": "latest-published-official-full-market-by-as_of-and-id-before-fixed-cutoff",
        "returns_inspected": False,
        "execution_evidence": {
            "status": "not_verified",
            "reason": "official_unadjusted_sessions_and_corporate_actions_required",
            "qfq_cache_is_execution_evidence": False,
        },
        "v6_activation_status": "unknown_requires_verified_activation_artifact",
    }
    try:
        with readonly_research_connection(Path(database)) as conn:
            runs = _selected_runs(conn, config)
            report["metadata_inventory"] = _metadata_inventory(conn)
            reports = [_audit_run(conn, run, config) for run in runs]
            report["batches"] = reports
            selected_ids = {int(run["id"]) for run in runs}
            missing = sorted(set(config.run_ids) - selected_ids)
            report["missing_requested_run_ids"] = missing
            report["status"] = "audited" if reports and not missing else "blocked"
            report["reason"] = None if reports and not missing else "requested_or_eligible_runs_unavailable"
    except (OSError, sqlite3.Error):
        report.update(status="blocked", reason="database_read_unavailable", batches=[])
    report["manifest_digest"] = _digest(report)
    return report


def read_research_session(conn: sqlite3.Connection, run_id: int) -> dict[str, object]:
    """Export a sealed complete snapshot, with prices kept outside the score inputs.

    The caller must supply a query-only connection in an explicit transaction.
    Seal verification and every persisted member, including missing/skipped
    rows, must share one snapshot even when retention deletes the live rows.
    It does not claim that absent official execution evidence has been verified.
    """
    if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise ValueError("research reads require PRAGMA query_only=ON")
    if not conn.in_transaction:
        raise ValueError("research reads require an explicit read transaction")
    verify_market_scan_snapshot(conn, run_id)
    run = conn.execute("SELECT * FROM market_scan_run WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError("research run is unavailable")
    rows = conn.execute(
        "SELECT * FROM market_scan_result WHERE run_id = ? ORDER BY rank IS NULL, rank, symbol", (run_id,),
    ).fetchall()
    return {"run": run_from_row(run).model_dump(mode="json"),
            "items": [result_from_row(row).model_dump(mode="json") for row in rows]}


def _selected_runs(conn: sqlite3.Connection, config: ResearchInputConfig) -> list[sqlite3.Row]:
    clauses = ["status IN ('success', 'degraded')", "mode = 'official'", "scope = ?", "data_date <= ?"]
    args: list[object] = [MARKET_SCAN_FULL_MARKET_SCOPE, config.as_of_date]
    if config.run_ids:
        clauses.append(f"id IN ({','.join('?' for _ in config.run_ids)})")
        args.extend(config.run_ids)
    rows = conn.execute(
        f"SELECT * FROM market_scan_run WHERE {' AND '.join(clauses)} ORDER BY as_of DESC, id DESC", args,
    ).fetchall()
    if config.run_ids:
        return rows
    # Maturity is determined from the fixed calendar only, before any PIT or
    # price inspection. Failed admission never causes replacement by another run.
    mature = [row for row in rows if _target_dates(str(row["quote_date"] or row["data_date"]), config)[1] is None]
    return mature[:config.max_runs]


def _metadata_inventory(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT mode, scope, rule_version, status, COUNT(*) AS run_count, "
        "MIN(data_date) AS first_data_date, MAX(data_date) AS last_data_date "
        "FROM market_scan_run GROUP BY mode, scope, rule_version, status "
        "ORDER BY mode, scope, rule_version, status",
    ).fetchall()
    return [dict(row) for row in rows]


def _audit_run(conn: sqlite3.Connection, run: sqlite3.Row, config: ResearchInputConfig) -> dict[str, object]:
    reasons: list[str] = []
    try:
        package = read_research_session(conn, int(run["id"]))
    except MarketScanSnapshotSealError:
        package = None
        reasons.append("snapshot_seal_invalid_or_missing")
    rows = conn.execute(
        "SELECT * FROM market_scan_result WHERE run_id = ? ORDER BY rank IS NULL, rank, symbol", (run["id"],),
    ).fetchall()
    statuses, pit_counts, member_reasons = _member_admission(rows, run)
    reasons.extend(member_reasons)
    target_dates, calendar_reason = _target_dates(str(run["quote_date"] or run["data_date"]), config)
    if calendar_reason:
        reasons.append(calendar_reason)
    ordering = [
        {"symbol": row["symbol"], "status": row["status"], "rank": row["rank"], "raw_score": row["raw_score"]}
        for row in rows
    ]
    return {
        "run_id": run["id"], "mode": run["mode"], "scope": run["scope"],
        "rule_version": run["rule_version"], "data_date": run["data_date"], "quote_date": run["quote_date"],
        "as_of": run["as_of"], "snapshot_digest": run["snapshot_digest"],
        "snapshot_prepared": package is not None and not reasons,
        "admission_status": "ready_for_score_research" if not reasons else "blocked",
        "reasons": reasons, "member_status_counts": dict(sorted(statuses.items())),
        "pit_status_counts": dict(sorted(pit_counts.items())),
        "member_ordering": ordering, "member_ordering_digest": _digest(ordering),
        "target_trading_dates": target_dates,
        "target_price_coverage": {"status": "not_inspected", "reason": "official_execution_evidence_required"},
    }


def _member_admission(rows: list[sqlite3.Row], run: sqlite3.Row) -> tuple[Counter[str], Counter[str], list[str]]:
    reasons = []
    statuses = Counter(str(row["status"]) for row in rows)
    successful = [row for row in rows if row["status"] == "success"]
    if len(rows) != int(run["total_count"]) or len(successful) != int(run["success_count"]):
        reasons.append("frozen_member_count_mismatch")
    if statuses.get("missing", 0) or int(run["missing_count"]):
        reasons.append("missing_frozen_members_block_shared_account_research")
    if any(row["rank"] != index for index, row in enumerate(successful, 1)):
        reasons.append("frozen_rank_sequence_invalid")
    pit_counts = Counter(_pit_status(row) for row in successful)
    if pit_counts.get("verified", 0) != len(successful):
        reasons.append("point_in_time_evidence_incomplete")
    if not successful:
        reasons.append("no_ranked_members")
    return statuses, pit_counts, reasons


def _target_dates(signal_date: str, config: ResearchInputConfig) -> tuple[list[str], str | None]:
    try:
        days = next_trade_dates(date.fromisoformat(signal_date), config.horizon + 1)
    except (ValueError, TradingCalendarCoverageError):
        return [], "trusted_calendar_unavailable"
    values = [value.isoformat() for value in days]
    if len(values) != config.horizon + 1:
        return values, "trusted_calendar_unavailable"
    # as_of_date means a completed end-of-day cutoff, never the process wall clock.
    return values, "target_dates_not_mature" if values[-1] > config.as_of_date else None


def _pit(row: sqlite3.Row) -> Mapping[str, object] | None:
    _metrics, details = decode_result_payload(row["metrics_json"])
    components = details.get("components")
    dimensions = components.get("score_dimensions") if isinstance(components, Mapping) else None
    evidence = dimensions.get("point_in_time_evidence") if isinstance(dimensions, Mapping) else None
    return evidence if isinstance(evidence, Mapping) else None


def _pit_status(row: sqlite3.Row) -> str:
    evidence = _pit(row)
    if evidence is None:
        return "missing"
    return "verified" if verify_market_scan_point_in_time_evidence(evidence) else "invalid"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
