"""Immutable local event ledger for causal, research-only delayed feedback."""

from __future__ import annotations

from app.services.market_scan_delayed_feedback_contracts import (
    FEEDBACK_VERSION, FeedbackConfig, FeedbackLedger, admit_feedback_events,
    admit_feedback_ledger, feedback_digest, feedback_json_bytes, feedback_time,
)
from app.services.market_scan_delayed_feedback_report import feedback_report
from app.services.market_scan_delayed_feedback_state import FeedbackState, apply_feedback_event


def create_feedback_ledger(config_payload: object, *, recorded_at: str) -> dict[str, object]:
    """Freeze configuration before accepting any prospective prediction."""
    config = FeedbackConfig.model_validate_json(feedback_json_bytes(config_payload), strict=True)
    return _ledger_payload(FeedbackState(config, feedback_time(recorded_at).isoformat()))


def append_feedback_events(
    ledger_payload: object, events_payload: object, *, expected_ledger_digest: str, recorded_at: str,
) -> dict[str, object]:
    """Verify once, then atomically calculate an ordered batch without mutating input."""
    ledger = admit_feedback_ledger(ledger_payload, expected_ledger_digest)
    state = _replay_ledger(ledger)
    for event in admit_feedback_events(events_payload):
        apply_feedback_event(state, event, recorded_at)
    return _ledger_payload(state)


def verify_feedback_ledger(payload: object, *, expected_ledger_digest: str) -> dict[str, object]:
    """Replay every original event; a caller pin is required even for local checks."""
    ledger = admit_feedback_ledger(payload, expected_ledger_digest)
    return _ledger_payload(_replay_ledger(ledger))


def _replay_ledger(ledger: FeedbackLedger) -> FeedbackState:
    state = FeedbackState(ledger.config, feedback_time(ledger.created_at).isoformat())
    for stored in ledger.events:
        apply_feedback_event(state, stored.event, stored.recorded_at)
        if state.records[-1] != stored:
            raise ValueError("feedback event chain or frozen state differs from full replay")
    if _ledger_payload(state) != ledger.model_dump(mode="json"):
        raise ValueError("feedback ledger metadata or summary differs from full replay")
    return state


def _ledger_payload(state: FeedbackState) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": FEEDBACK_VERSION, "config": state.config.model_dump(mode="json"),
        "config_digest": feedback_digest(state.config.model_dump(mode="json")), "created_at": state.created_at,
        "events": [record.model_dump(mode="json") for record in state.records], "event_count": len(state.records),
        "head_digest": state.records[-1].event_digest if state.records else state.genesis_digest(),
        "final_state_digest": state.state_digest(), "summary": feedback_report(state),
        "provenance": {"authority": "unverified_local_event_chain", "official_pit_verified": False,
                       "global_registration_verified": False, "clock": "caller_supplied_in_pure_API;internal_UTC_in_CLI"},
        "promotion_eligible": False,
        "limitations": ["hashes and caller timestamps do not authenticate external provenance or prove global completeness",
                        "independent expected digest protects the selected local ledger prefix only",
                        "all predictions and labels are self-asserted; no source or realized-return authentication",
                        "rolling signed bias is an unproven research candidate; frozen comparator shares the same labels",
                        "same-day close targets are probability diagnostics, not evidence of T+1 executable returns",
                        "classification and Top100 flags are frozen declarations, not reconstructed production eligibility",
                        "target dates use the covered local exchange calendar; no external calendar attestation"],
    }
    payload["ledger_digest"] = feedback_digest(payload)
    return payload
