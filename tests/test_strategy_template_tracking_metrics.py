from dataclasses import replace
from datetime import date
import math

import pytest

from app.services.strategy_template_tracking_metrics import (
    STRATEGY_TEMPLATE_TRACKING_IDS, StrategyTrackingPosition, StrategyTrackingSelection,
    StrategyTrackingSession, build_strategy_template_tracking_report,
)
from app.services.trading_calendar import next_trade_dates


def _selection(template, value=.10, *, count=2, weight=.20):
    positions = tuple(StrategyTrackingPosition(f"{index:06d}.SZ", f"样本{index}", "行业一", weight, 10, value)
                      for index in range(count))
    return StrategyTrackingSelection(
        template, "ready", 100, count, positions, weight * count, 600_000, 300,
        "c" * 64, "d" * 64, "available", value * weight * count, count,
    )


def _session(day="2026-08-03", run_id=1, *, baseline=.05, momentum=.10, low_vol=.08, count=2):
    return StrategyTrackingSession(
        run_id, day, next_trade_dates(date.fromisoformat(day), 10)[-1].isoformat(), "rule-v1", "a" * 64, "b" * 64,
        f"{day}T16:00:00+08:00",
        tuple(_selection(template, value, count=count, weight=.4 / count)
              for template, value in zip(STRATEGY_TEMPLATE_TRACKING_IDS, (baseline, momentum, low_vol), strict=True)),
    )


def _report(sessions, **updates):
    args = dict(templates=[{"template_id": key} for key in STRATEGY_TEMPLATE_TRACKING_IDS],
                as_of="2026-09-19T16:00:00+08:00", completed_date="2026-09-18", horizon=10,
                notional_cash_cny=1_000_000, source={"read_only": True, "provider_calls": 0})
    args.update(updates)
    return build_strategy_template_tracking_report(sessions, **args)


def _replace_selection(session, index, **updates):
    selections = list(session.selections)
    selections[index] = replace(selections[index], **updates)
    return replace(session, selections=tuple(selections))


def test_fixed_weights_are_not_renormalized_and_cost_estimates_are_not_deducted():
    report = _report([_session()])
    comparison = report["cohorts"][0]["comparisons"][0]
    assert comparison["candidate_average_return"] == pytest.approx(.04)
    assert comparison["baseline_average_return"] == pytest.approx(.02)
    assert comparison["candidate_minus_baseline_return"] == pytest.approx(.02)
    row = report["cohorts"][0]["sessions"][0]["selections"][1]
    assert row["unallocated_weight"] == pytest.approx(.60)
    assert row["industry_weights"] == {"行业一": .4}
    assert report["costs_deducted"] is False
    assert report["continuous_portfolio_simulation"] is False
    assert comparison["confidence_interval_95"] is None
    assert report["prediction_accuracy_validated"] is report["promotion_eligible"] is False


def test_same_dates_are_paired_before_averaging_and_missing_stock_is_not_dropped():
    first = _session(momentum=.80)
    baseline = first.selections[0]
    positions = (replace(baseline.positions[0], forward_return=None, outcome_reason="missing_bar"), baseline.positions[1])
    first = _replace_selection(first, 0, positions=positions, outcome_status="missing", weighted_gross_return=None,
                               available_outcome_count=1, missing_reason_counts={"missing_bar": 1})
    second = _session("2026-08-04", 2, baseline=.10, momentum=.20)
    report = _report([first, second])
    row = report["cohorts"][0]["comparisons"][0]
    assert row["paired_dates"] == ["2026-08-04"]
    assert row["candidate_average_return"] == pytest.approx(.08)
    assert row["baseline_average_return"] == pytest.approx(.04)
    assert row["excluded_pair_counts"] == {"candidate:available|baseline:missing": 1}
    assert report["cohorts"][0]["template_summaries"][0]["outcome_counts"] == {"available": 1, "missing": 1}


def test_dates_have_equal_weight_regardless_of_number_of_selected_stocks():
    report = _report([_session(momentum=.10), _session("2026-08-04", 2, momentum=.30, count=20)])
    row = report["cohorts"][0]["comparisons"][0]
    assert row["candidate_average_return"] == pytest.approx((.04 + .12) / 2)
    assert row["paired_session_count"] == 2
    assert row["mean_selected_overlap_count"] == 11


@pytest.mark.parametrize("status", ["pending", "missing", "no_selection", "calendar_unavailable", "blocked"])
def test_unavailable_and_cash_only_outcomes_never_become_successful_zero_returns(status):
    session = _session()
    updates = dict(positions=(), target_invested_weight=0, available_outcome_count=0,
                   outcome_status=status, weighted_gross_return=None)
    if status == "no_selection":
        updates["status"] = "no_trade"
    if status == "blocked":
        updates["status"] = "blocked"
    session = _replace_selection(session, 1, **updates)
    report = _report([session])
    row = report["cohorts"][0]["comparisons"][0]
    assert row["paired_session_count"] == 0
    assert row["candidate_average_return"] is row["candidate_minus_baseline_return"] is None
    assert row["excluded_pair_counts"] == {f"candidate:{status}|baseline:available": 1}


def test_zero_and_negative_returns_remain_observations():
    report = _report([_session(baseline=0, momentum=-.10, low_vol=0)])
    rows = report["cohorts"][0]["comparisons"]
    assert rows[0]["paired_session_count"] == rows[1]["paired_session_count"] == 1
    assert rows[0]["candidate_minus_baseline_return"] == pytest.approx(-.04)
    assert rows[1]["candidate_minus_baseline_return"] == 0


def test_contracts_are_never_pooled_and_sessions_are_sorted():
    first, second = _session(), _session("2026-08-04", 2)
    third = replace(_session(run_id=3), rule_version="rule-v2", score_spec_hash="e" * 64)
    report = _report([second, third, first])
    assert len(report["cohorts"]) == 2
    assert report["cohorts"][0]["comparisons"][0]["paired_dates"] == ["2026-08-03", "2026-08-04"]
    assert report["cohorts"][1]["comparisons"][0]["paired_session_count"] == 1


def test_failures_and_exclusions_are_retained_without_claiming_validated_performance():
    failure = {"run_id": 2, "reason": "broken_seal"}
    excluded = {"run_id": 3, "reason": "same_contract_session_rescan"}
    report = _report([_session()], run_failures=[failure], excluded_runs=[excluded])
    assert report["status"] == "insufficient_data"
    assert report["run_failures"] == [failure]
    assert report["excluded_runs"] == [excluded]
    assert report["statistical_validation"] == "not_performed"
    assert _report([])["cohorts"] == []
    assert _report([])["status"] == "insufficient_data"


@pytest.mark.parametrize("mutation", [
    {"status": "winner"}, {"outcome_status": "winner"}, {"target_invested_weight": math.nan},
    {"target_invested_weight": 1.1}, {"target_invested_weight": .8}, {"weighted_gross_return": math.inf},
    {"weighted_gross_return": None}, {"weighted_gross_return": .10}, {"available_outcome_count": 1},
    {"outcome_status": "missing", "weighted_gross_return": 0}, {"status": "no_trade"},
])
def test_inconsistent_selection_contract_is_rejected(mutation):
    with pytest.raises(ValueError):
        _report([_replace_selection(_session(), 1, **mutation)])


@pytest.mark.parametrize("updates", [
    {"target_weight": -1}, {"target_weight": math.nan}, {"target_weight": True},
    {"signal_price": 0}, {"signal_price": math.inf}, {"forward_return": math.nan},
    {"forward_return": -1.01},
])
def test_invalid_position_cannot_reach_aggregate(updates):
    session = _session()
    selected = session.selections[1]
    positions = (replace(selected.positions[0], **updates), selected.positions[1])
    with pytest.raises(ValueError):
        _report([_replace_selection(session, 1, positions=positions)])


def test_duplicate_members_dates_and_missing_template_cannot_inflate_samples():
    session = _session()
    selected = session.selections[1]
    duplicate = _replace_selection(session, 1, positions=(selected.positions[0], selected.positions[0]))
    cases = [[session, replace(session, run_id=2)], [session, replace(session, signal_date="2026-08-04")],
             [duplicate], [replace(session, selections=session.selections[:2])],
             [replace(session, score_spec_hash="unknown")], [replace(session, target_date=session.signal_date)]]
    for rows in cases:
        with pytest.raises(ValueError):
            _report(rows)


@pytest.mark.parametrize("kwargs", [
    {"templates": []}, {"horizon": True}, {"horizon": 0}, {"horizon": 7}, {"horizon": 10.0},
    {"notional_cash_cny": math.nan}, {"notional_cash_cny": 10**400}, {"notional_cash_cny": 1},
])
def test_invalid_report_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        _report([_session()], **kwargs)


@pytest.mark.parametrize("kwargs", [
    {"as_of": "2026-09-19T16:00:00"}, {"completed_date": "2026-09-17"}, {"horizon": 5},
    {"as_of": "2026-08-04T16:00:00+08:00", "completed_date": "2026-08-04"},
    {"as_of": "2026-08-02T16:00:00+08:00", "completed_date": "2026-07-31"},
])
def test_maturity_cutoff_and_horizon_are_bound_to_report_observation(kwargs):
    with pytest.raises(ValueError):
        _report([_session()], **kwargs)


@pytest.mark.parametrize("field", ["estimated_round_trip_cost_cny", "residual_cash_cny"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -1, True])
def test_invalid_cash_and_cost_are_rejected_at_metrics_boundary(field, value):
    with pytest.raises(ValueError, match="invalid cash or cost estimate"):
        _report([_replace_selection(_session(), 1, **{field: value})])


def test_extreme_finite_values_do_not_overflow_when_averaged():
    sessions = [_session(f"2026-08-{day:02d}", day, momentum=1e308) for day in range(3, 8)]
    sessions = [_replace_selection(row, 1, estimated_round_trip_cost_cny=1e308) for row in sessions]
    report = _report(sessions)
    comparison = report["cohorts"][0]["comparisons"][0]
    assert comparison["candidate_average_return"] == pytest.approx(4e307)
    assert comparison["candidate_minus_baseline_return"] == pytest.approx(4e307)
    assert report["cohorts"][0]["template_summaries"][1]["mean_estimated_round_trip_cost_cny"] == 1e308
