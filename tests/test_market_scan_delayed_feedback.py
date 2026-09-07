from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.market_scan_delayed_feedback import (
    append_feedback_events, create_feedback_ledger, verify_feedback_ledger,
)


CREATED = "2026-08-24T08:00:00+08:00"
DECISION = "2026-08-24T10:00:00+08:00"
END = "2026-08-24T15:00:00+08:00"


def config(**changes):
    return {
        "event_definition": {"horizon_sessions": 0, "target_offset_sessions": 0,
                             "return_definition": "close_return", "benchmark_definition": "zero_return",
                             "threshold": 0.0, "operator": "gt", "conditioning_digest": "a" * 64},
        "model_digest": "b" * 64, "update_rule": "rolling_signed_bias",
        "baseline_bias": 0.0, "window_labels": 100, "minimum_labels": 1,
        "minimum_dates": 1, "reference_base_rate": .5, **changes,
    }


def prediction(identifier="p1", **changes):
    return {"kind": "prediction", "prediction_id": identifier, "symbol": "600519.SH",
            "cohort_id": "cohort1", "source_digest": "c" * 64,
            "signal_date": "2026-08-24", "decision_at": DECISION, "label_end_at": END,
            "raw_probability": .4, "selected_top100": True, "industry": "消费", "regime": "rising", **changes}


def label(identifier="p1", **changes):
    return {"kind": "label", "prediction_id": identifier, "label_end_at": END,
            "available_at": "2026-08-24T15:05:00+08:00", "realized_return": .02,
            "benchmark_return": 0.0, "source_digest": "d" * 64, **changes}


def append(ledger, events, at=DECISION):
    return append_feedback_events(ledger, events, expected_ledger_digest=ledger["ledger_digest"], recorded_at=at)


def seeded(**settings):
    return append(create_feedback_ledger(config(**settings), recorded_at=CREATED), [prediction()])


def test_prediction_before_label_is_frozen_and_label_consumed_once() -> None:
    first = seeded()
    frozen = deepcopy(first["events"][0])
    final = append(first, [label()], "2026-08-24T15:06:00+08:00")
    assert final["events"][0] == frozen
    assert final["events"][0]["applied_probability"] == .4
    assert final["events"][1]["state_before_digest"] != final["events"][1]["state_after_digest"]
    assert verify_feedback_ledger(final, expected_ledger_digest=final["ledger_digest"]) == final
    assert final["summary"]["matured_label_count"] == 1
    assert final["promotion_eligible"] is False
    with pytest.raises(ValueError, match="duplicate|consumed"):
        append(final, [label()], "2026-08-24T15:07:00+08:00")


def test_online_update_only_changes_later_predictions_and_preserves_fixed_comparator() -> None:
    labelled = append(seeded(), [label()], "2026-08-24T15:06:00+08:00")
    next_prediction = prediction("p2", signal_date="2026-08-25", decision_at="2026-08-25T10:00:00+08:00",
                                 label_end_at="2026-08-25T15:00:00+08:00", raw_probability=.2)
    result = append(labelled, [next_prediction], "2026-08-25T10:00:00+08:00")
    assert result["events"][-1]["baseline_probability"] == .2
    assert result["events"][-1]["applied_probability"] == pytest.approx(.8)
    fixed = append(seeded(update_rule="fixed_baseline"), [label()], "2026-08-24T15:06:00+08:00")
    assert append(fixed, [next_prediction], "2026-08-25T10:00:00+08:00")["events"][-1]["applied_probability"] == .2


@pytest.mark.parametrize("bad", [
    label("unknown"), label(available_at="2026-08-24T14:59:59+08:00"),
    label(available_at="2026-08-24T15:07:00+08:00"), label(label_end_at="2026-08-25T15:00:00+08:00"),
])
def test_early_unknown_and_wrong_endpoint_labels_are_rejected(bad) -> None:
    with pytest.raises(ValueError):
        append(seeded(), [bad], "2026-08-24T15:06:00+08:00")


@pytest.mark.parametrize("changes", [
    {"raw_probability": float("nan")}, {"raw_probability": float("inf")},
    {"raw_probability": True}, {"raw_probability": "0.4"}, {"raw_probability": 1.1},
    {"decision_at": "2026-08-24T10:00:00"}, {"label_end_at": "2026-08-24T15:00:00"},
    {"signal_date": "2026-08-23"}, {"industry": ""}, {"selected_top100": 1},
])
def test_invalid_prediction_inputs_fail_closed(changes) -> None:
    with pytest.raises(ValueError):
        append(create_feedback_ledger(config(), recorded_at=CREATED), [prediction(**changes)])


def test_prediction_must_be_prospective_and_cannot_use_state_newer_than_decision() -> None:
    empty = create_feedback_ledger(config(), recorded_at=CREATED)
    with pytest.raises(ValueError):
        append(empty, [prediction()], END)
    with pytest.raises(ValueError):
        append(empty, [prediction(decision_at="2026-08-24T11:00:00+08:00")])
    labelled = append(seeded(), [label()], "2026-08-24T15:06:00+08:00")
    stale = prediction("p2", label_end_at="2026-08-25T15:00:00+08:00")
    with pytest.raises(ValueError):
        append(labelled, [stale], "2026-08-24T15:07:00+08:00")


def test_digest_pins_chain_tampering_omission_and_clock_rollback_rejected() -> None:
    ledger = append(seeded(), [label()], "2026-08-24T15:06:00+08:00")
    for field in ("applied_probability", "state_before_digest", "previous_event_digest", "recorded_at"):
        changed = deepcopy(ledger)
        changed["events"][0][field] = .9 if field == "applied_probability" else "e" * 64
        with pytest.raises(ValueError):
            verify_feedback_ledger(changed, expected_ledger_digest=ledger["ledger_digest"])
    changed = deepcopy(ledger)
    changed["events"].pop()
    with pytest.raises(ValueError):
        verify_feedback_ledger(changed, expected_ledger_digest=ledger["ledger_digest"])
    with pytest.raises(ValueError):
        append(ledger, [prediction("p3")], CREATED)


def test_batch_is_atomic_and_duplicate_forecasts_do_not_gain_extra_labels() -> None:
    original = create_feedback_ledger(config(), recorded_at=CREATED)
    before = deepcopy(original)
    with pytest.raises(ValueError):
        append(original, [prediction(), prediction()])
    assert original == before
    with pytest.raises(ValueError, match="duplicate"):
        append(original, [prediction(), prediction("different-id")])


def test_timezone_equivalent_endpoints_mature_on_same_instant() -> None:
    ledger = seeded()
    final = append(ledger, [label(label_end_at="2026-08-24T07:00:00Z", available_at="2026-08-24T07:05:00Z")],
                   "2026-08-24T07:06:00Z")
    assert final["summary"]["matured_label_count"] == 1
    json.dumps(final, allow_nan=False)


def test_minimum_dates_and_labels_apply_to_state_and_report() -> None:
    result = append(seeded(minimum_labels=2, minimum_dates=2), [label()], "2026-08-24T15:06:00+08:00")
    assert result["summary"]["all"]["status"] == "insufficient_data"
    assert result["summary"]["current_update"]["bias"] == 0
    assert result["summary"]["current_update"]["status"] == "insufficient_data"


def test_resealed_derived_state_still_must_match_full_replay() -> None:
    ledger = seeded()
    changed = deepcopy(ledger)
    changed["events"][0]["applied_probability"] = .8
    changed["ledger_digest"] = sha256_hex(canonical_json_bytes({k: v for k, v in changed.items() if k != "ledger_digest"}))
    with pytest.raises(ValueError):
        verify_feedback_ledger(changed, expected_ledger_digest=changed["ledger_digest"])


def test_cross_day_labels_update_in_recorded_order_and_window_evicts_once() -> None:
    initial = create_feedback_ledger(config(window_labels=1, event_definition={**config()["event_definition"],
                                                                          "horizon_sessions": 1, "target_offset_sessions": 1}),
                                     recorded_at=CREATED)
    early = prediction(label_end_at="2026-08-25T15:00:00+08:00")
    second = prediction("p2", symbol="000001.SZ", label_end_at="2026-08-25T15:00:00+08:00", raw_probability=.8,
                        selected_top100=False, industry=None, regime=None)
    first = append(initial, [early, second])
    label_two = label("p2", label_end_at=second["label_end_at"], available_at="2026-08-25T15:05:00+08:00", realized_return=-.1)
    label_one = label(label_end_at=early["label_end_at"], available_at="2026-08-25T15:02:00+08:00")
    final = append(first, [label_two, label_one], "2026-08-25T15:06:00+08:00")
    assert final["summary"]["current_update"]["window_label_count"] == 1
    assert final["summary"]["current_update"]["bias"] == pytest.approx(.6)
    assert final["summary"]["all"]["observation_count"] == 2
    assert final["summary"]["top100"]["observation_count"] == 1
    assert final["summary"]["strata"]["all"]["industry"][-1]["values"] == [None]
    for previous, current in zip(final["events"], final["events"][1:], strict=False):
        assert previous["state_after_digest"] == current["state_before_digest"]
    assert verify_feedback_ledger(final, expected_ledger_digest=final["ledger_digest"]) == final


def test_frozen_threshold_baseline_and_clipping_are_applied_exactly() -> None:
    settings = config(baseline_bias=.8)
    settings["event_definition"]["operator"] = "ge"
    result = append(create_feedback_ledger(settings, recorded_at=CREATED), [prediction()])
    assert result["events"][0]["baseline_probability"] == 1
    final = append(result, [label(realized_return=0.0)], "2026-08-24T15:06:00+08:00")
    assert final["events"][-1]["outcome"] == 1
    other = append(seeded(), [label(realized_return=0.0)], "2026-08-24T15:06:00+08:00")
    assert other["events"][-1]["outcome"] == 0


@pytest.mark.parametrize("settings", [
    {"minimum_dates": 2}, {"minimum_labels": 101}, {"window_labels": 0},
    {"baseline_bias": float("nan")}, {"baseline_bias": True}, {"reference_base_rate": float("inf")},
    {"model_digest": "bad"}, {"update_rule": "auto-promote"}, {"extra": True},
])
def test_invalid_frozen_configuration_rejected(settings) -> None:
    with pytest.raises(ValueError):
        create_feedback_ledger(config(**settings), recorded_at=CREATED)


@pytest.mark.parametrize("changes", [
    {"target_offset_sessions": 2}, {"horizon_sessions": True}, {"threshold": float("-inf")},
])
def test_invalid_event_definition_rejected(changes) -> None:
    definition = {**config()["event_definition"], **changes}
    with pytest.raises(ValueError):
        create_feedback_ledger(config(event_definition=definition), recorded_at=CREATED)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, ".02"])
def test_bad_realized_return_is_never_consumed(value) -> None:
    with pytest.raises(ValueError):
        append(seeded(), [label(realized_return=value)], "2026-08-24T15:06:00+08:00")


def test_finite_operands_cannot_overflow_benchmark_relative_return() -> None:
    with pytest.raises(ValueError, match="finite"):
        append(seeded(), [label(realized_return=1e308, benchmark_return=-1e308)], "2026-08-24T15:06:00+08:00")


def test_late_prediction_cannot_steal_a_later_model_update() -> None:
    initial = create_feedback_ledger(config(event_definition={**config()["event_definition"], "target_offset_sessions": 1}),
                                     recorded_at=CREATED)
    first = append(initial, [prediction(label_end_at="2026-08-25T15:00:00+08:00")])
    first = append(first, [label(label_end_at="2026-08-25T15:00:00+08:00", available_at="2026-08-25T15:05:00+08:00")],
                   "2026-08-25T15:06:00+08:00")
    stale = prediction("p2", signal_date="2026-08-25", decision_at="2026-08-25T10:00:00+08:00",
                       label_end_at="2026-08-26T15:00:00+08:00")
    with pytest.raises(ValueError, match="predates"):
        append(first, [stale], "2026-08-25T15:07:00+08:00")


@pytest.mark.parametrize("field,value", [
    ("event_count", 55), ("summary", {}), ("promotion_eligible", 0), ("head_digest", "f" * 64),
])
def test_resealed_metadata_does_not_override_full_replay(field, value) -> None:
    changed = seeded()
    changed[field] = value
    changed["ledger_digest"] = sha256_hex(canonical_json_bytes({k: v for k, v in changed.items() if k != "ledger_digest"}))
    with pytest.raises(ValueError):
        verify_feedback_ledger(changed, expected_ledger_digest=changed["ledger_digest"])


def test_strict_clock_endpoint_and_event_batch_boundaries(monkeypatch) -> None:
    from app.services import market_scan_delayed_feedback_contracts as contracts

    empty = create_feedback_ledger(config(), recorded_at=CREATED)
    for at in ("2026-08-24 08:00:00+08:00", "2026-08-24T08:00:00", "bad"):
        with pytest.raises(ValueError):
            create_feedback_ledger(config(), recorded_at=at)
    for event in (prediction(label_end_at="2026-08-25T15:00:00+08:00"), prediction(signal_date="20260824")):
        with pytest.raises(ValueError):
            append(empty, [event])
    with pytest.raises(ValueError):
        append(empty, [])
    with pytest.raises(ValueError):
        verify_feedback_ledger(empty, expected_ledger_digest="bad")
    monkeypatch.setattr(contracts, "MAX_LEDGER_BYTES", 1)
    with pytest.raises(ValueError, match="byte limit"):
        create_feedback_ledger(config(), recorded_at=CREATED)


def test_stored_boolean_outcome_is_not_an_integer_label() -> None:
    changed = append(seeded(), [label()], "2026-08-24T15:06:00+08:00")
    changed["events"][-1]["outcome"] = True
    with pytest.raises(ValueError):
        verify_feedback_ledger(changed, expected_ledger_digest=changed["ledger_digest"])
