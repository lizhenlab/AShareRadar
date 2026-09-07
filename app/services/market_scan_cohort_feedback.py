"""Immutable complete-date feedback experiments, independent of legacy v1."""

from __future__ import annotations

from datetime import datetime
import math

from app.services.market_scan_cohort_feedback_calendar import FrozenCohortCalendar, cohort_date
from app.services.market_scan_cohort_feedback_contracts import (
    COHORT_FEEDBACK_VERSION, CohortFeedbackConfig, CohortFeedbackLedger, cohort_events, planned_dates,
)
from app.services.market_scan_cohort_feedback_state import CohortState, apply_cohort_event
from app.services.market_scan_delayed_feedback_contracts import feedback_digest, feedback_json_bytes, feedback_time, feedback_market_date
from app.services.market_scan_probability_metrics import evaluate_probability_predictions


def create_cohort_feedback_ledger(config_payload: object, calendar_payload: object, *, recorded_at: str) -> dict[str, object]:
    config = CohortFeedbackConfig.model_validate_json(feedback_json_bytes(config_payload), strict=True)
    calendar = FrozenCohortCalendar.model_validate_json(feedback_json_bytes(calendar_payload), strict=True)
    return cohort_ledger_payload(initialize_cohort_state(config, calendar, recorded_at))


def initialize_cohort_state(config: CohortFeedbackConfig, calendar: FrozenCohortCalendar, created_at: str) -> CohortState:
    created = feedback_time(created_at)
    planned_dates(config, calendar)
    if feedback_time(calendar.captured_at) > created or feedback_market_date(created_at) > cohort_date(config.signal_start):
        raise ValueError("calendar and signal-date plan must be frozen before forecasting starts")
    return CohortState(config, calendar, created.isoformat())


def append_cohort_feedback_events(
    ledger_payload: object, events_payload: object, *, expected_ledger_digest: str, recorded_at: str,
) -> dict[str, object]:
    state = replay_cohort_ledger(ledger_payload, expected_ledger_digest)
    for event in cohort_events(events_payload):
        apply_cohort_event(state, event, recorded_at)
    return cohort_ledger_payload(state)


def verify_cohort_feedback_ledger(payload: object, *, expected_ledger_digest: str) -> dict[str, object]:
    return cohort_ledger_payload(replay_cohort_ledger(payload, expected_ledger_digest))


def replay_cohort_ledger(payload: object, expected_digest: str) -> CohortState:
    encoded = feedback_json_bytes(payload)
    ledger = CohortFeedbackLedger.model_validate_json(encoded, strict=True)
    if not isinstance(payload, dict) or ledger.ledger_digest != expected_digest:
        raise ValueError("independent cohort ledger digest mismatch")
    if feedback_digest({key: value for key, value in payload.items() if key != "ledger_digest"}) != expected_digest:
        raise ValueError("cohort ledger bytes differ from pinned digest")
    state = initialize_cohort_state(ledger.config, ledger.calendar, ledger.created_at)
    for record in ledger.events:
        apply_cohort_event(state, record.event, record.recorded_at)
        if feedback_json_bytes(state.events[-1].model_dump(mode="json")) != feedback_json_bytes(record.model_dump(mode="json")):
            raise ValueError("cohort derived types or event chain differs from replay")
    if feedback_json_bytes(cohort_ledger_payload(state)) != encoded:
        raise ValueError("cohort summary, types or metadata differs from complete replay")
    return state


def cohort_ledger_payload(state: CohortState) -> dict[str, object]:
    at = feedback_time(state.events[-1].recorded_at if state.events else state.created_at)
    payload: dict[str, object] = {
        "schema_version": COHORT_FEEDBACK_VERSION, "config": state.config.model_dump(mode="json"),
        "calendar": state.calendar.model_dump(mode="json"), "created_at": state.created_at,
        "events": [event.model_dump(mode="json") for event in state.events], "final_state_digest": state.digest(),
        "summary": {"as_of": at.isoformat(), "current_update": state.update(at),
                    "declared_cohort_count": len(state.cohorts), "prediction_count": len(state.predictions),
                    "consumed_label_count": sum(len(track.labels) for track in state.cohorts.values()),
                    "diagnostics": cohort_diagnostics(state, at)},
        "promotion_eligible": False,
        "provenance": {"authority": "unverified_local_cohort_chain", "calendar": "raw_bytes_frozen_local_calendar;no_external_attestation",
                       "external_source_authenticated": False, "global_completeness_verified": False,
                       "scope": "only_predeclared_date_range_and_frozen_members", "legacy_migration": "none",
                       "interpretation": "research_only;no_conformal_coverage_or_performance_improvement_claim",
                       "clock": "caller_supplied_in_pure_API;internal_UTC_in_CLI"},
    }
    payload["ledger_digest"] = feedback_digest(payload)
    return payload


def cohort_diagnostics(state: CohortState, at: datetime) -> dict[str, object]:
    days: list[dict[str, object]] = []
    briers: list[float] = []
    for day, track in sorted(state.cohorts.items()):
        if not track.event.members or track.complete_mean is None or track.last_received_at is None:
            continue
        if feedback_time(track.last_received_at) > at:
            continue
        ids = sorted(track.labels)
        dates = [day] * len(ids)
        labels = [track.labels[key][1] for key in ids]
        fixed = [state.predictions[key].baseline_probability for key in ids]
        candidate = [state.predictions[key].applied_probability for key in ids]
        briers.append(math.fsum((probability - target) ** 2 for probability, target in zip(candidate, labels, strict=True)) / len(ids))
        days.append({"signal_date": day, "fixed_baseline": evaluate_probability_predictions(fixed, labels, dates, base_rate=state.config.reference_base_rate),
                     "recorded_candidate": evaluate_probability_predictions(candidate, labels, dates, base_rate=state.config.reference_base_rate)})
    return {"status": "descriptive_complete_cohorts_only", "days": days,
            "candidate_brier_equal_date": math.fsum(briers) / len(briers) if briers else None,
            "selection_limit": "inspect_all_matured_gaps;complete_cohorts_can_be_a_selected_subset"}
