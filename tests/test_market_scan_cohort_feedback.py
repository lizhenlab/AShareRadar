from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from unittest.mock import patch

import pytest

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.market_scan_cohort_feedback import create_cohort_feedback_ledger, append_cohort_feedback_events, verify_cohort_feedback_ledger
from app.services.market_scan_cohort_feedback_calendar import capture_cohort_calendar
from tests.test_market_scan_delayed_feedback import config as old_config


START = "2026-08-24T08:00:00+08:00"


def freeze_calendar_fixture(monkeypatch, tmp_path):
    from app.services import market_scan_cohort_feedback_calendar as calendar

    raw = json.loads(calendar.BUNDLED_CALENDAR_PATH.read_text())
    raw["updated_at"] = "2026-07-01 00:00:00"
    target = tmp_path / "calendar-fixture.json"
    target.write_bytes(canonical_json_bytes(raw))
    monkeypatch.setattr(calendar, "CALENDAR_PATH", target)
    monkeypatch.setattr(calendar, "BUNDLED_CALENDAR_PATH", target)


@pytest.fixture(autouse=True)
def frozen_calendar_source(monkeypatch, tmp_path):
    freeze_calendar_fixture(monkeypatch, tmp_path)


def config(**changes):
    old = old_config()
    old.pop("window_labels")
    old.pop("minimum_dates")
    return {**old, "signal_start": "2026-08-24", "signal_end": "2026-08-28",
            "window_dates": 3, "minimum_complete_dates": 1, **changes}


def member(identifier="p1", symbol="600519.SH", probability=.4):
    return {"prediction_id": identifier, "symbol": symbol, "raw_probability": probability,
            "selected_top100": True, "industry": "消费", "regime": "rising"}


def cohort(day="2026-08-24", members=None):
    return {"kind": "prediction_cohort", "cohort_id": day, "signal_date": day,
            "decision_at": day + "T10:00:00+08:00", "source_digest": "c" * 64,
            "members": [member()] if members is None else members}


def label(identifier="p1", day="2026-08-24", positive=True):
    return {"kind": "label", "prediction_id": identifier, "label_end_at": day + "T15:00:00+08:00",
            "available_at": day + "T15:05:00+08:00", "realized_return": .02 if positive else -.02,
            "benchmark_return": 0.0, "source_digest": "d" * 64}


def empty(**changes):
    calendar = capture_cohort_calendar("2026-08-24", "2026-09-01", captured_at=START)
    return create_cohort_feedback_ledger(config(**changes), calendar, recorded_at=START)


def add(ledger, events, at):
    return append_cohort_feedback_events(ledger, events, expected_ledger_digest=ledger["ledger_digest"], recorded_at=at)


def test_calendar_changes_cannot_change_frozen_replay() -> None:
    ledger = add(empty(), [cohort()], "2026-08-24T10:00:00+08:00")
    with patch("app.services.trading_calendar.next_trade_dates", return_value=(date(2026, 8, 26),)), \
            patch("app.services.trading_calendar.trading_date_range", side_effect=AssertionError("replay touched current calendar")):
        assert verify_cohort_feedback_ledger(ledger, expected_ledger_digest=ledger["ledger_digest"]) == ledger


def test_label_order_does_not_change_next_cohort_probabilities() -> None:
    ledger = add(empty(), [cohort(members=[member(), member("p2", "000001.SZ")])], "2026-08-24T10:00:00+08:00")
    outputs = []
    for events in ([label(), label("p2", positive=False)], [label("p2", positive=False), label()]):
        labelled = add(ledger, events, "2026-08-24T15:06:00+08:00")
        final = add(labelled, [cohort("2026-08-25", [member("p3")])], "2026-08-25T10:00:00+08:00")
        outputs.append(final["events"][-1]["derived"]["predictions"][0]["applied_probability"])
    assert outputs == pytest.approx([.5, .5])


def test_incomplete_and_undeclared_matured_days_withhold_updates() -> None:
    ledger = add(empty(), [cohort(members=[member(), member("p2", "000001.SZ")])], "2026-08-24T10:00:00+08:00")
    ledger = add(ledger, [label()], "2026-08-24T15:06:00+08:00")
    ledger = add(ledger, [cohort("2026-08-26", [member("p3")])], "2026-08-26T10:00:00+08:00")
    update = ledger["events"][-1]["derived"]["update"]
    assert update["status"] == "withheld_matured_gaps" and update["bias"] == 0
    assert [day["status"] for day in update["days"]] == ["incomplete", "undeclared"]
    assert ledger["events"][-1]["derived"]["predictions"][0]["applied_probability"] == .4


def test_daily_stock_replication_does_not_change_cross_day_weight() -> None:
    biases = []
    for count in (1, 10):
        rows = [member(f"p{index}", f"{600000 + index:06d}.SH") for index in range(count)]
        ledger = add(empty(), [cohort(members=rows)], "2026-08-24T10:00:00+08:00")
        ledger = add(ledger, [label(item["prediction_id"]) for item in rows], "2026-08-24T15:06:00+08:00")
        ledger = add(ledger, [cohort("2026-08-25", [member("negative")])], "2026-08-25T10:00:00+08:00")
        ledger = add(ledger, [label("negative", "2026-08-25", False)], "2026-08-25T15:06:00+08:00")
        final = add(ledger, [cohort("2026-08-26", [member("next")])], "2026-08-26T10:00:00+08:00")
        biases.append(final["events"][-1]["derived"]["update"]["bias"])
    assert biases == pytest.approx([.1, .1])


def test_future_days_and_complete_date_window_are_explicit() -> None:
    ledger = add(empty(window_dates=1), [cohort()], "2026-08-24T10:00:00+08:00")
    assert ledger["events"][-1]["derived"]["update"]["status"] == "insufficient_data"
    ledger = add(ledger, [label()], "2026-08-24T15:06:00+08:00")
    ledger = add(ledger, [cohort("2026-08-25", [member("p2")])], "2026-08-25T10:00:00+08:00")
    ledger = add(ledger, [label("p2", "2026-08-25", False)], "2026-08-25T15:06:00+08:00")
    ledger = add(ledger, [cohort("2026-08-26", [member("p3")])], "2026-08-26T10:00:00+08:00")
    update = ledger["events"][-1]["derived"]["update"]
    assert [row["signal_date"] for row in update["days"]] == ["2026-08-25"]
    assert update["bias"] == -.4
    assert ledger["promotion_eligible"] is False
    json.dumps(ledger, allow_nan=False)


def test_member_freeze_single_label_consumption_and_resealed_types() -> None:
    original = add(empty(), [cohort()], "2026-08-24T10:00:00+08:00")
    frozen = deepcopy(original["events"][0])
    with pytest.raises(ValueError):
        add(original, [cohort(members=[member("replacement")])], "2026-08-24T11:00:00+08:00")
    final = add(original, [label()], "2026-08-24T15:06:00+08:00")
    assert final["events"][0] == frozen
    with pytest.raises(ValueError):
        add(final, [label()], "2026-08-24T15:07:00+08:00")
    changed = deepcopy(final)
    changed["events"][0]["derived"]["update"]["bias"] = False
    changed["ledger_digest"] = sha256_hex(canonical_json_bytes({key: value for key, value in changed.items() if key != "ledger_digest"}))
    with pytest.raises(ValueError):
        verify_cohort_feedback_ledger(changed, expected_ledger_digest=changed["ledger_digest"])


def test_missing_old_dates_cannot_be_hidden_by_window_rollover() -> None:
    ledger = add(empty(window_dates=1), [cohort("2026-08-25", [member("p2")])], "2026-08-25T10:00:00+08:00")
    ledger = add(ledger, [label("p2", "2026-08-25")], "2026-08-25T15:06:00+08:00")
    ledger = add(ledger, [cohort("2026-08-26", [member("p3")])], "2026-08-26T10:00:00+08:00")
    update = ledger["events"][-1]["derived"]["update"]
    assert [row["signal_date"] for row in update["days"]] == ["2026-08-25"]
    assert update["matured_gap_dates"] == ["2026-08-24"] and update["bias"] == 0


def test_empty_date_is_declared_without_fabricating_labels() -> None:
    ledger = add(empty(), [cohort(members=[])], "2026-08-24T10:00:00+08:00")
    ledger = add(ledger, [cohort("2026-08-25", [member("p2")])], "2026-08-25T10:00:00+08:00")
    update = ledger["events"][-1]["derived"]["update"]
    assert update["days"][0]["status"] == "empty" and update["matured_gap_dates"] == []
    assert update["status"] == "insufficient_data" and update["complete_nonempty_dates"] == 0


def test_decision_cannot_use_labels_received_later_even_in_same_append() -> None:
    ledger = add(empty(), [cohort()], "2026-08-24T10:00:00+08:00")
    next_cohort = cohort("2026-08-25", [member("p2")])
    late_label = {**label(), "available_at": "2026-08-25T11:00:00+08:00"}
    ledger = add(ledger, [late_label, next_cohort], "2026-08-25T11:01:00+08:00")
    assert ledger["events"][-1]["derived"]["update"]["status"] == "withheld_matured_gaps"
    assert ledger["events"][-1]["derived"]["predictions"][0]["applied_probability"] == .4


def test_fixed_baseline_and_minimum_complete_dates_never_activate_early() -> None:
    for settings in ({"update_rule": "fixed_baseline"}, {"minimum_complete_dates": 2}, {"minimum_labels": 2}):
        ledger = add(empty(**settings), [cohort()], "2026-08-24T10:00:00+08:00")
        ledger = add(ledger, [label()], "2026-08-24T15:06:00+08:00")
        ledger = add(ledger, [cohort("2026-08-25", [member("p2")])], "2026-08-25T10:00:00+08:00")
        assert ledger["events"][-1]["derived"]["update"]["bias"] == 0


@pytest.mark.parametrize("settings", [
    {"signal_start": "2026-08-23"}, {"signal_start": "20260824"}, {"signal_end": "2026-08-21"},
    {"minimum_complete_dates": 4}, {"window_dates": 0}, {"baseline_bias": float("nan")},
    {"reference_base_rate": True}, {"model_digest": "invalid"},
])
def test_bad_plan_configuration_is_rejected(settings) -> None:
    with pytest.raises(ValueError):
        empty(**settings)


@pytest.mark.parametrize("changes", [
    {"raw_probability": True}, {"raw_probability": float("nan")}, {"raw_probability": float("inf")},
    {"raw_probability": "0.4"}, {"symbol": "bad"}, {"selected_top100": 1}, {"industry": ""},
])
def test_strict_cohort_member_inputs(changes) -> None:
    with pytest.raises(ValueError):
        add(empty(), [cohort(members=[{**member(), **changes}])], "2026-08-24T10:00:00+08:00")


def test_cohort_identity_and_prospective_date_admission() -> None:
    initial = empty()
    for event, at in ((cohort(members=[member(), member()]), "2026-08-24T10:00:00+08:00"),
                      (cohort(), "2026-08-24T15:00:00+08:00"),
                      ({**cohort(), "decision_at": "2026-08-25T10:00:00+08:00"}, "2026-08-24T10:00:00+08:00"),
                      (cohort("2026-09-01"), "2026-09-01T10:00:00+08:00")):
        with pytest.raises(ValueError):
            add(initial, [event], at)
    ledger = add(initial, [cohort()], "2026-08-24T10:00:00+08:00")
    with pytest.raises(ValueError):
        add(ledger, [cohort("2026-08-25")], "2026-08-25T10:00:00+08:00")
    with pytest.raises(ValueError):
        add(ledger, [{**cohort("2026-08-25", [member("p2")]), "cohort_id": "2026-08-24"}], "2026-08-25T10:00:00+08:00")
    with pytest.raises(ValueError):
        add(ledger, [label()], START)
    with pytest.raises(ValueError):
        add(ledger, [], "2026-08-24T11:00:00+08:00")


@pytest.mark.parametrize("changes", [
    {"prediction_id": "unknown"}, {"label_end_at": "2026-08-25T15:00:00+08:00"},
    {"available_at": "2026-08-24T15:07:00+08:00"}, {"available_at": "2026-08-24T14:00:00+08:00"},
    {"realized_return": float("inf")}, {"realized_return": True},
    {"realized_return": 1e308, "benchmark_return": -1e308},
])
def test_bad_labels_cannot_change_cohort_state(changes) -> None:
    ledger = add(empty(), [cohort()], "2026-08-24T10:00:00+08:00")
    original = deepcopy(ledger)
    with pytest.raises(ValueError):
        add(ledger, [{**label(), **changes}], "2026-08-24T15:06:00+08:00")
    assert ledger == original


def test_threshold_equality_clipping_and_horizon_binding() -> None:
    settings = config()
    settings["event_definition"] = {**settings["event_definition"], "operator": "ge", "horizon_sessions": 1, "target_offset_sessions": 2}
    ledger = empty(event_definition=settings["event_definition"], baseline_bias=.8)
    ledger = add(ledger, [cohort()], "2026-08-24T10:00:00+08:00")
    assert ledger["events"][0]["derived"]["label_end_at"] == "2026-08-26T15:00:00+08:00"
    assert ledger["events"][0]["derived"]["predictions"][0]["baseline_probability"] == 1
    ledger = add(ledger, [{**label(day="2026-08-26"), "realized_return": 0.0}], "2026-08-26T15:06:00+08:00")
    assert ledger["events"][-1]["derived"]["outcome"] == 1


@pytest.mark.parametrize("field,value", [("promotion_eligible", True), ("summary", {}), ("final_state_digest", "f" * 64)])
def test_resealed_ledger_metadata_and_pins_are_verified(field, value) -> None:
    ledger = empty()
    with pytest.raises(ValueError):
        verify_cohort_feedback_ledger(ledger, expected_ledger_digest="f" * 64)
    changed = deepcopy(ledger)
    changed[field] = value
    with pytest.raises(ValueError):
        verify_cohort_feedback_ledger(changed, expected_ledger_digest=ledger["ledger_digest"])
    changed["ledger_digest"] = sha256_hex(canonical_json_bytes({key: value for key, value in changed.items() if key != "ledger_digest"}))
    with pytest.raises(ValueError):
        verify_cohort_feedback_ledger(changed, expected_ledger_digest=changed["ledger_digest"])


@pytest.mark.parametrize("corruption", ["raw_digest", "sessions", "coverage", "package_digest", "raw_count", "raw_order", "updated_future"])
def test_calendar_bytes_metadata_sessions_and_capture_time_are_bound(corruption) -> None:
    calendar = capture_cohort_calendar("2026-08-24", "2026-09-01", captured_at=START)
    if corruption.startswith("raw_") and corruption != "raw_digest" or corruption == "updated_future":
        raw = json.loads(calendar["raw_file_utf8"])
        if corruption == "raw_count":
            raw["trade_date_count"] -= 1
        elif corruption == "raw_order":
            raw["trade_dates"].reverse()
        else:
            raw["updated_at"] = "2099-01-01 00:00:00"
        calendar["raw_file_utf8"] = json.dumps(raw)
        calendar["raw_file_digest"] = sha256_hex(calendar["raw_file_utf8"].encode())
        calendar["calendar_digest"] = sha256_hex(canonical_json_bytes({key: value for key, value in calendar.items() if key != "calendar_digest"}))
    else:
        key, value = {"raw_digest": ("raw_file_digest", "f" * 64), "sessions": ("sessions", ["2026-08-24"]),
                      "coverage": ("coverage_end", "2099-01-01"), "package_digest": ("calendar_digest", "f" * 64)}[corruption]
        calendar[key] = value
    with pytest.raises(ValueError):
        create_cohort_feedback_ledger(config(), calendar, recorded_at=START)
