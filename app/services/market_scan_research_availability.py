"""Bounded, read-only availability audits over a previously declared calendar."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
import re
import sqlite3

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.db.market_scan_integrity import MarketScanSnapshotSealError
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE, MarketScanResultItem, MarketScanRun
from app.repositories.market_scan_mapping import run_from_row
from app.services.market_scan_research_availability_contract import (
    AVAILABILITY_PLAN_VERSION, AVAILABILITY_REPORT_VERSION, AvailabilityBatch, AvailabilityMember, AvailabilityPlan, availability_timestamp,
)
from app.services.market_scan_research_availability_statistics import (
    availability_groups, availability_missing_streaks, availability_predecision_differences, availability_status_counts,
)
from app.services.market_scan_research_inputs import read_research_session, readonly_research_connection
from app.services.market_scan_score_dimensions import verify_market_scan_point_in_time_evidence_context


def summarize_availability_snapshots(plan: AvailabilityPlan, snapshots: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Summarize synthetic inputs; caller-supplied seals never confer database trust."""
    supplied: dict[int, Mapping[str, object]] = {}
    runs = []
    for snapshot in snapshots:
        if set(snapshot) != {"run", "items"}:
            raise ValueError("availability snapshots require exactly run and items")
        run = MarketScanRun.model_validate(snapshot["run"])
        if run.id in supplied:
            raise ValueError("duplicate or conflicting supplied run")
        supplied[run.id] = snapshot
        runs.append(run)
    selected = _select_runs(plan, runs)
    batches = [_prepare_batch(supplied[run.id], run) for run in selected]
    return _report(plan, batches, selected, {}, verified=False)


def audit_market_scan_availability(database: str | Path, plan: AvailabilityPlan) -> dict[str, object]:
    """Verify only selected sealed batches in one existing consistent read snapshot."""
    try:
        with readonly_research_connection(Path(database).expanduser()) as conn:
            selected = _select_runs(plan, _read_bounded_runs(conn, plan))
            batches, failures = _read_selected_batches(conn, selected)
    except (OSError, sqlite3.Error):
        return _report(plan, [], [], {day: "database_read_unavailable" for day in plan.trading_dates}, verified=True)
    return _report(plan, batches, selected, failures, verified=True)


def _read_bounded_runs(conn: sqlite3.Connection, plan: AvailabilityPlan) -> list[MarketScanRun]:
    runs: list[MarketScanRun] = []
    for day in plan.trading_dates:
        statement = "SELECT * FROM market_scan_run WHERE data_date = ? AND mode = ? AND scope = ?"
        parameters: list[object] = [day, plan.mode, MARKET_SCAN_FULL_MARKET_SCOPE]
        if plan.run_ids:
            statement += " AND id IN (" + ",".join("?" for _ in plan.run_ids) + ")"
            parameters.extend(plan.run_ids)
        else:
            statement += " AND status IN ('success', 'degraded')"
        runs.extend(run_from_row(row) for row in conn.execute(statement, parameters))
    return runs


def _select_runs(plan: AvailabilityPlan, runs: Sequence[MarketScanRun]) -> list[MarketScanRun]:
    selected: dict[str, MarketScanRun] = {}
    for run in runs:
        if not _matches_plan(run, plan):
            continue
        prior = selected.get(run.data_date)
        if prior is not None and plan.selection_policy == "explicit-run-ids":
            raise ValueError("explicit selection requires one run per date")
        if prior is None or (availability_timestamp(run.as_of), run.id) > (availability_timestamp(prior.as_of), prior.id):
            selected[run.data_date] = run
    return [selected[day] for day in plan.trading_dates if day in selected]


def _matches_plan(run: MarketScanRun, plan: AvailabilityPlan) -> bool:
    if run.data_date not in plan.trading_dates or run.mode != plan.mode or run.scope != MARKET_SCAN_FULL_MARKET_SCOPE:
        return False
    if plan.run_ids and run.id not in plan.run_ids:
        return False
    if not plan.run_ids and run.status not in {"success", "degraded"}:
        return False
    cutoff = availability_timestamp(plan.selection_cutoff)
    timestamps = (run.as_of, run.created_at, run.updated_at, run.finished_at, run.snapshot_sealed_at)
    return all(availability_timestamp(value) <= cutoff for value in timestamps if value is not None)


def _read_selected_batches(
    conn: sqlite3.Connection, selected: Sequence[MarketScanRun],
) -> tuple[list[AvailabilityBatch], dict[str, str]]:
    batches: list[AvailabilityBatch] = []
    failures: dict[str, str] = {}
    for run in selected:
        try:
            snapshot = read_research_session(conn, run.id)
            batches.append(_prepare_batch(snapshot, run))
        except (MarketScanSnapshotSealError, ValueError):
            failures[run.data_date] = "snapshot_integrity_or_membership_invalid"
    return batches, failures


def _prepare_batch(snapshot: Mapping[str, object], run: MarketScanRun) -> AvailabilityBatch:
    if not isinstance(snapshot["items"], list):
        raise ValueError("items must be the complete frozen member array")
    items = [MarketScanResultItem.model_validate(item) for item in snapshot["items"]]
    if len(items) != run.total_count or len({item.symbol for item in items}) != len(items):
        raise ValueError("entire unique frozen membership is required")
    members = tuple(_member(item, run) for item in sorted(items, key=lambda item: item.symbol))
    counts = availability_status_counts(members)
    expected = {"success": run.success_count, "missing": run.missing_count, "skipped": run.skipped_count,
                "pending": run.total_count - run.processed_count}
    if counts != expected:
        raise ValueError("frozen member status counts must conserve the declared denominator")
    if run.status in {"success", "degraded"} and sorted(item.rank or 0 for item in items if item.status == "success") != list(range(1, run.success_count + 1)):
        raise ValueError("published success ranks must form the complete frozen sequence")
    return AvailabilityBatch(run.data_date, run.id, str(run.snapshot_digest or ""), run.snapshot_seal_origin, members)


def _member(item: MarketScanResultItem, run: MarketScanRun) -> AvailabilityMember:
    if item.run_id != run.id or re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", item.symbol) is None:
        raise ValueError("member identity does not match the declared run")
    if item.symbol != f"{item.code}.{item.market}":
        raise ValueError("member symbol/code/market conflict")
    if item.data_date is not None and (
        date.fromisoformat(item.data_date).isoformat() != item.data_date or item.data_date > run.data_date
        or item.status == "success" and item.data_date != run.data_date
    ):
        raise ValueError("member data_date conflicts with the frozen run")
    if availability_timestamp(item.updated_at) > availability_timestamp(run.updated_at):
        raise ValueError("member timestamp exceeds the frozen run")
    return AvailabilityMember(
        item.symbol, item.status, item.market, _label(item.industry), _label(item.metadata_source),
        _label(item.quote_source), _label(item.kline_source), _error_category(item), _predecision_amount(item, run),
    )


def _label(value: str | None) -> str:
    return value.strip() if value is not None and value.strip() else "UNKNOWN"


def _error_category(item: MarketScanResultItem) -> str:
    if item.status == "success":
        return "none"
    text = (item.error or item.reason or "").lower()
    rules = (("timeout", r"timeout|timed out|超时"), ("rate_limit", r"429|quota|限流|配额"),
             ("invalid_data", r"invalid|conflict|不一致|冲突|无效|非法"), ("unavailable", r"missing|unavailable|缺失|缺少|不可用"))
    return next((name for name, pattern in rules if re.search(pattern, text)), "unclassified" if text else "not_recorded")


def _predecision_amount(item: MarketScanResultItem, run: MarketScanRun) -> float | None:
    components = item.score_details.get("components")
    dimensions = components.get("score_dimensions") if isinstance(components, Mapping) else None
    evidence = dimensions.get("point_in_time_evidence") if isinstance(dimensions, Mapping) else None
    if not isinstance(evidence, Mapping) or item.quote_observed_at is None:
        return None
    if availability_timestamp(item.quote_observed_at) > availability_timestamp(run.as_of):
        return None
    valid = verify_market_scan_point_in_time_evidence_context(
        evidence, item=item, expected_data_date=run.data_date, expected_quote_date=run.quote_date,
        expected_as_of=run.as_of, expected_mode=run.mode, require_action_eligible=False,
    )
    return item.amount if valid else None


def _report(
    plan: AvailabilityPlan, batches: Sequence[AvailabilityBatch], selected: Sequence[MarketScanRun],
    failures: Mapping[str, str], *, verified: bool,
) -> dict[str, object]:
    by_date = {batch.data_date: batch for batch in batches}
    selected_ids = {run.data_date: run.id for run in selected}
    missing_ids = sorted(set(plan.run_ids) - set(selected_ids.values()))
    days = [_day_report(day, by_date.get(day), selected_ids.get(day), failures.get(day), verified) for day in plan.trading_dates]
    plan_payload = {"schema_version": AVAILABILITY_PLAN_VERSION, **asdict(plan),
                    "trading_dates": list(plan.trading_dates), "run_ids": list(plan.run_ids)}
    report: dict[str, object] = {
        "schema_version": AVAILABILITY_REPORT_VERSION, "plan": plan_payload,
        "plan_digest": sha256_hex(canonical_json_bytes(plan_payload)), "returns_inspected": False,
        "promotion_eligible": False, "research_admission": "unchanged; this audit does not admit strategies",
        "status": "blocked" if failures else "audited" if len(batches) == len(plan.trading_dates) and not missing_ids else "incomplete",
        "days": days, "missing_requested_run_ids": missing_ids,
        "missing_streaks": availability_missing_streaks(plan.trading_dates, batches),
        "summary": {"calendar_session_count": len(plan.trading_dates), "selected_session_count": len(selected),
                    "member_observed_session_count": len(batches), "unknown_session_count": len(plan.trading_dates) - len(batches),
                    "known_member_observation_count": sum(len(batch.members) for batch in batches)},
        "limitations": ["frozen sources and error categories are descriptive, not independent provider certification",
                        "unknown dates, pending or absent members interrupt explicit missing streaks; they are not recoveries",
                        "selection calendar is declared for this audit, not externally timestamped preregistration",
                        "no forward outcomes, execution source verification or missing-at-random assumption"],
    }
    report["digest"] = sha256_hex(canonical_json_bytes(report))
    return report


def _day_report(
    day: str, batch: AvailabilityBatch | None, run_id: int | None, failure: str | None, verified: bool,
) -> dict[str, object]:
    if batch is None:
        return {"data_date": day, "run_id": run_id, "source_status": "unverified",
                "reason": failure or "no_selected_batch", "member_count": None, "status_counts": None,
                "observed_member_fraction": None, "groups": {}, "predecision_differences": {"status": "insufficient_evidence"}}
    counts = availability_status_counts(batch.members)
    return {
        "data_date": day, "run_id": batch.run_id, "snapshot_digest": batch.snapshot_digest,
        "snapshot_seal_origin": batch.seal_origin,
        "source_status": "database_snapshot_verified" if verified else "synthetic_unverified", "reason": None,
        "member_count": len(batch.members), "status_counts": counts,
        "observed_member_fraction": counts["success"] / len(batch.members) if batch.members else None,
        "groups": availability_groups(batch.members),
        "predecision_differences": availability_predecision_differences(batch.members),
        "member_statuses": [{"symbol": item.symbol, "status": item.status} for item in batch.members],
        "strict_member_admission": "blocked" if counts["missing"] or counts["pending"] else "other_research_gates_not_evaluated",
    }


__all__ = ["AvailabilityPlan", "audit_market_scan_availability", "summarize_availability_snapshots"]
