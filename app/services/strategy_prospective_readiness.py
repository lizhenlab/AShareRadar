"""Read-only, layered diagnostics; never collect or rehabilitate a past signal."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, time
from pathlib import Path
import sqlite3
from typing import Any, cast

from app.artifacts.io import ArtifactIOError
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE
from app.services.strategy_prospective_collection import first_published_strategy_scan, freeze_strategy_prospective_source
from app.services.strategy_prospective_freshness import prospective_source_freshness
from app.services.strategy_prospective_plan import read_strategy_prospective_state, strategy_prospective_status
from app.services.strategy_template_tracking import strategy_template_input_checks
from app.services.trading_calendar import TradingCalendarCoverageError
from app.utils.clock import ASHARE_TIMEZONE, utc_now


READINESS_SCHEMA_VERSION = "strategy-template-prospective-readiness-v2"
_OFFICIAL_MATERIALS = (
    "official_registry", "independently_verified_registry_digest", "official_raw_root", "official_session_directory",
)
_DATA_ERRORS = (ArtifactIOError, OSError, ValueError, TypeError, KeyError, OverflowError, sqlite3.Error)


def strategy_prospective_readiness(database: Path, root: Path, plan_id: str) -> dict[str, object]:
    """Inspect retained identities, one production source and the frozen date axis.

    A production input can pass diagnosis during source drift, but cannot thereby
    become a captured sample. No execution source is discovered or loaded here.
    """
    now = utc_now()
    report: dict[str, object] = {
        "schema_version": READINESS_SCHEMA_VERSION, "operation": "readiness", "read_only": True,
        "plan_id": plan_id, "plan_digest": None, "as_of": now.isoformat(),
        "formal_execution": {
            "status": "not_assessed", "required_materials": list(_OFFICIAL_MATERIALS),
            "missing_or_unverified_materials": list(_OFFICIAL_MATERIALS),
            "assessment_scope": "materials_not_supplied_no_filesystem_discovery",
            "official_execution_admitted": False, "net_returns_available": False,
        },
        "adoptable_template_id": None, "promotion_eligible": False,
    }
    try:
        plan, receipts = read_strategy_prospective_state(root, plan_id, verify_code=False)
        status = strategy_prospective_status(root, plan_id)
        head = receipts[-1]["digest"] if receipts else plan["digest"]
        if status["plan_digest"] != plan["digest"] or status["head_digest"] != head:
            raise ValueError("readiness receipt prefix changed during observation")
    except _DATA_ERRORS as exc:
        report.update(
            framework={"status": "unavailable", "plan_receipt_integrity": "unverified", "error_type": type(exc).__name__,
                       "source_code_matches": None, "calendar_matches_frozen": None,
                       "capture_blockers": ["plan_or_receipt_validation_failed"]},
            basket_input={"status": "not_assessed", "completeness_status": "not_assessed",
                          "reason": "verified_plan_unavailable", "diagnostic_only": True,
                          "current_readiness": {"status": "unknown", "pending_conditions": ["verified_plan_and_receipts_required"]}},
            sample_maturity={"status": "not_assessed", "reason": "verified_plan_unavailable"},
        )
        return report
    spec = cast(dict[str, Any], plan["specification"])
    blockers = [name for key, name in (("source_code_matches", "source_drift"),
                                       ("calendar_matches_frozen", "calendar_drift")) if not status[key]]
    report.update(
        plan_digest=plan["digest"],
        framework={
            "status": "blocked" if blockers else "matched", "plan_status": status["status"],
            "plan_receipt_integrity": "verified", "source_code_matches": status["source_code_matches"],
            "calendar_matches_frozen": status["calendar_matches_frozen"], "capture_blockers": blockers,
            "runtime_dependencies_and_configuration": "not_attested_by_source_manifest",
            "snapshot_archive_integrity": "not_assessed", "head_digest": head,
            "receipt_count": len(receipts), "next_signal_date": status["next_signal_date"],
            "due_count": status["due_count"], "overdue_count": status["overdue_count"],
            "receipt_counts": status["receipt_counts"],
        },
        basket_input=_basket_input(database, spec, now, blockers),
        sample_maturity=_sample_maturity(spec, receipts, now),
    )
    return report


def _basket_input(database: Path, spec: Mapping[str, Any], now: datetime, blockers: Sequence[str]) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "no_source", "diagnostic_only": True, "creates_prospective_sample": False,
        "selection_policy": "latest-published-date-then-earliest-official-full-market-run-no-fallback",
        "frozen_score_spec_hash": spec["production_score_spec_hash"],
        "capture_blockers": list(blockers), "checks": {}, "missing_classifications": [],
        "freshness": prospective_source_freshness(None, now),
    }
    try:
        with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
            row = _latest_source(conn, now)
            if row is None:
                return _current_input_readiness(result)
            result.update(run_id=int(row["id"]), signal_date=row["data_date"],
                          freshness=prospective_source_freshness(str(row["data_date"]), now),
                          source_result_counts={key: row[key] for key in ("total_count", "success_count", "missing_count", "skipped_count")})
            rows = conn.execute("SELECT * FROM market_scan_result WHERE run_id=? ORDER BY rank,symbol LIMIT 10001", (row["id"],)).fetchall()
            if len(rows) > 10_000:
                return _current_input_readiness({**result, "status": "unavailable", "reason": "source_result_limit_exceeded"})
            result["missing_classifications"] = _missing_classifications(rows)
            result["missing_reason_counts"] = dict(Counter(str(item["category"]) for item in cast(list[dict[str, object]], result["missing_classifications"])))
            result["checks"] = strategy_template_input_checks(conn, row, rows, as_of=now, score_spec_hash=spec["production_score_spec_hash"])
            frozen = freeze_strategy_prospective_source(conn, row, spec, now)
            result.update(status="admissible" if frozen["status"] == "captured" else "source_incomplete",
                          admission_reason=frozen["reason"], source_available_at=frozen["source_available_at"])
            session = frozen.get("session")
            result["diagnostic_template_count"] = len(session["selections"]) if isinstance(session, dict) else 0
    except TradingCalendarCoverageError:
        freshness = cast(dict[str, object], result["freshness"])
        result.update(status="freshness_unknown" if freshness["status"] == "unknown" else "source_integrity_unknown",
                      completeness_status="unknown", reason="source_validation_calendar_coverage_required")
    except _DATA_ERRORS as exc:
        result.update(status="unavailable", reason="production_input_diagnosis_failed", error_type=type(exc).__name__)
    return _current_input_readiness(result)


def _current_input_readiness(result: dict[str, object]) -> dict[str, object]:
    completeness = str(result.get("completeness_status", result["status"]))
    freshness = cast(dict[str, object], result["freshness"])
    current = str(freshness["status"])
    blockers = cast(list[str], result["capture_blockers"])
    pending = list(blockers)
    requirements = {"no_source": "published_source_required", "source_incomplete": "first_published_source_must_pass_frozen_input_contract",
                    "unavailable": "production_input_diagnosis_required", "unknown": "source_integrity_assessment_required"}
    if completeness in requirements:
        pending.append(requirements[completeness])
    recency_requirements = {"unknown": "trusted_calendar_coverage_required", "stale": "expected_published_session_source_required",
                           "ahead_of_expected": "source_must_match_expected_published_session"}
    if current in recency_requirements:
        pending.append(recency_requirements[current])
    if completeness == "unknown" and current != "unknown":
        pending.append("source_validation_calendar_coverage_required")
    status = "ready" if not pending else "blocked"
    if not blockers and current not in {"stale", "ahead_of_expected"} and (
        completeness in {"unavailable", "unknown"} or completeness == "admissible" and current == "unknown"
    ):
        status = "unknown"
    source_status = {"stale": "source_stale", "unknown": "freshness_unknown", "ahead_of_expected": "source_ahead_of_expected"}
    result.update(
        completeness_status=completeness,
        status=source_status.get(current, completeness) if completeness == "admissible" else result["status"],
        current_readiness={"status": status, "pending_conditions": pending,
                           "assessment_scope": "input_integrity_freshness_and_frozen_identity_only_not_capture_or_execution"},
    )
    return result


def _latest_source(conn: sqlite3.Connection, now: datetime) -> sqlite3.Row | None:
    days = conn.execute(
        """SELECT DISTINCT data_date FROM market_scan_run
           WHERE mode='official' AND scope=? AND status IN ('success','degraded') AND data_date<=?
           ORDER BY data_date DESC LIMIT 201""",
        (MARKET_SCAN_FULL_MARKET_SCOPE, now.astimezone(ASHARE_TIMEZONE).date().isoformat()),
    ).fetchall()
    for item in days[:200]:
        row = first_published_strategy_scan(conn, item["data_date"], now)
        if row is not None:
            return row
    if len(days) > 200:
        raise ValueError("readiness source search exceeds bounded date window")
    return None


def _missing_classifications(rows: Sequence[sqlite3.Row]) -> list[dict[str, object]]:
    """Describe recorded input failures; never infer exchange/tradability facts."""
    result = []
    for row in rows:
        if row["status"] != "missing":
            continue
        reason = str(row["reason"] or row["error"] or "")
        if reason.startswith(("日K停留在 ", "当日报价存在有效成交，但日K仅到 ")):
            category = "daily_bar_behind_required_session"
        elif reason == "当日全天单一价格，无法确认开盘可成交性":
            category = "single_price_entry_execution_unknown"
        elif reason in ("当日日K成交量为 0 且报价不可用，可能停牌", "当日报价与日K均无有效成交，可能停牌"):
            category = "no_valid_turnover_exchange_state_unknown"
        else:
            category = "unclassified_recorded_failure"
        result.append({"symbol": row["symbol"], "status": "missing", "category": category,
                       "classification_basis": "recorded_reason_only", "exchange_session_state": "unknown",
                       "entry_execution_state": "unknown", "exclusion_authorized": False})
    return result


def _sample_maturity(spec: Mapping[str, Any], receipts: Sequence[Mapping[str, object]], now: datetime) -> dict[str, object]:
    local = now.astimezone(ASHARE_TIMEZONE)
    completed = local.date().isoformat()
    before_publication = local.time() < time(15, 15)
    by_day = {str(row["trade_date"]): row for row in receipts}
    anchors = []
    for day in spec["signal_dates"]:
        receipt = by_day.get(day)
        exit_day = spec["exit_dates"][day]
        maturity = "calendar_unavailable" if exit_day is None else (
            "mature" if exit_day < completed or (exit_day == completed and not before_publication) else "pending")
        anchors.append({"signal_date": day, "exit_date": exit_day, "maturity": maturity,
                        "receipt_status": receipt["status"] if receipt else "not_recorded",
                        "captured": receipt is not None and receipt["status"] == "captured"})
    captured = [row for row in anchors if row["captured"]]
    counts = Counter(str(row["maturity"]) for row in captured)
    return {
        "status": "captured_receipts_present" if captured else "needs_captured_baskets",
        "basis": "verified_receipt_chain_and_frozen_exit_dates_not_replayed_execution_results",
        "planned_anchor_count": len(anchors), "captured_anchor_count": len(captured),
        "captured_template_basket_count": len(captured) * len(spec["templates"]),
        "mature_captured_anchor_count": counts["mature"], "pending_captured_anchor_count": counts["pending"],
        "calendar_unavailable_captured_anchor_count": counts["calendar_unavailable"],
        "anchors": anchors, "minimum_complete_anchors": spec["inference_contract"]["minimum_complete_anchors"],
        "formal_net_outcome_count": None, "prospective_statistical_evaluation": "not_implemented",
    }
