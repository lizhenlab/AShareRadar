from copy import deepcopy
from datetime import date
import math

import pytest

from app.services import strategy_template_selection as selection
from app.services.strategy_template_tracking_metrics import STRATEGY_TEMPLATE_TRACKING_IDS as IDS
from app.services.trading_calendar import TradingCalendarCoverageError, trading_dates_between


@pytest.fixture(scope="module")
def days():
    return trading_dates_between(date(2025, 1, 2), date(2026, 9, 18), allow_auto_refresh=False)


def _outcome(template, value, *, stress=None, drawdown=-.01, status="available"):
    stress = value - .001 if stress is None else stress
    base = {"status": status, "net_return": value if status == "available" else None,
            "independent_batch_max_drawdown": drawdown if status == "available" else None}
    stressed = {**base, "net_return": stress if status == "available" else None}
    return {"template_id": template, "status": status, "net_return": base["net_return"],
            "stress_net_return": stressed["net_return"], "base": base, "stress": stressed}


def _report(days, *, count=20, horizon=1, values=(0, .03, .01), start=0):
    indexes = [start + index * (horizon + 1) for index in range(count)]
    sessions = [{
        "run_id": index + 1, "signal_date": days[offset].isoformat(),
        "entry_date": days[offset + 1].isoformat(), "exit_date": days[offset + horizon + 1].isoformat(),
        "snapshot_digest": "c" * 64,
        "selections": [_outcome(template, value) for template, value in zip(IDS, values, strict=True)],
    } for index, offset in enumerate(indexes)]
    return {
        "schema_version": "strategy-template-net-returns-v1", "as_of": "2026-09-19T16:00:00+08:00",
        "as_of_completed_date": "2026-09-18", "horizon_sessions": horizon,
        "entry_policy": "D+1-open", "exit_policy": "D+H+1-close",
        "return_unit": "decimal_fraction", "notional_cash_cny": 1_000_000,
        "continuous_portfolio_simulation": False, "promotion_eligible": False,
        "execution_evidence": {"provenance_status": "official_raw_file_verified", "manifest_digest": "d" * 64},
        "cost_specs": [{"template_id": item, "strategy_fingerprint": str(index) * 64}
                       for index, item in enumerate(IDS)],
        "cohorts": [{"rule_version": "test-rule-v1", "score_spec_hash": "a" * 64, "sessions": sessions}],
    }


def _build(report, **kwargs):
    return selection.build_strategy_template_selection_report(report, **kwargs)


def _cohort(report, **kwargs):
    return _build(report, **kwargs)["cohorts"][0]


def _sessions(report):
    return report["cohorts"][0]["sessions"]


def test_clear_historical_net_winner_is_never_an_adoptable_strategy(days):
    report = _build(_report(days))
    cohort = report["cohorts"][0]
    assert cohort["complete_anchor_count"] == 20
    assert cohort["diagnostic_leader_id"] == cohort["statistical_winner_id"] == IDS[1]
    assert cohort["risk_qualified_winner_id"] == IDS[1]
    assert cohort["adoptable_template_id"] is report["adoptable_template_id"] is None
    assert report["promotion_eligible"] is False
    assert report["status"] == "historical_evidence_available"
    assert "independent_prospective_validation_missing" in report["adoption_blockers"]
    assert "continuous_capital_and_position_validation_missing" in report["adoption_blockers"]
    assert len(cohort["comparisons"]) == 3
    assert all(item["holm_adjusted_p_value"] < .05 for item in cohort["comparisons"])
    assert report["inference_contract"]["bootstrap_samples"] == 2000


def test_nineteen_anchors_can_lead_descriptively_but_cannot_pass_inference(days):
    cohort = _cohort(_report(days, count=19))
    assert cohort["status"] == "insufficient_data"
    assert cohort["diagnostic_leader_id"] == IDS[1]
    assert cohort["statistical_winner_id"] is None
    assert "minimum_complete_anchors_not_met" in cohort["blockers"]
    assert all(item["two_sided_p_value"] is None for item in cohort["comparisons"])


def test_calendar_anchors_do_not_compress_when_a_mature_session_is_missing(days):
    report = _report(days, count=21)
    missing = _sessions(report).pop(8)["signal_date"]
    cohort = _cohort(report)
    assert len(cohort["planned_anchor_dates"]) == 21
    assert cohort["complete_anchor_count"] == 20
    assert cohort["missing_anchor_dates"] == [missing]
    assert cohort["diagnostic_leader_id"] is cohort["statistical_winner_id"] is None
    assert all(item["two_sided_p_value"] is None for item in cohort["comparisons"])


@pytest.mark.parametrize("status", ["pending", "unavailable", "blocked"])
def test_unknown_one_template_blocks_all_pairwise_inference_without_survivor_selection(days, status):
    report = _report(days, count=21)
    unavailable = _sessions(report)[8]
    unavailable["selections"][1] = _outcome(IDS[1], -1, status=status)
    cohort = _cohort(report)
    assert cohort["complete_anchor_count"] == 20
    assert cohort["missing_anchor_dates"] == [unavailable["signal_date"]]
    assert len(cohort["comparisons"]) == 3
    assert all(item["holm_adjusted_p_value"] is None for item in cohort["comparisons"])


def test_a_failed_original_source_cannot_be_replaced_by_a_success_on_the_same_day(days):
    report = _report(days, count=21)
    day = _sessions(report)[2]["signal_date"]
    failure = {"rule_version": "test-rule-v1", "score_spec_hash": "a" * 64, "signal_date": day}
    cohort = _cohort(report, run_failures=[failure])
    assert cohort["missing_anchor_dates"] == [day]
    assert cohort["statistical_winner_id"] is None


def test_failed_first_signal_fixes_calendar_anchor_phase_before_any_return_is_read(days):
    report = _report(days, start=1)
    failure = {"rule_version": "test-rule-v1", "score_spec_hash": "a" * 64, "signal_date": days[0].isoformat()}
    cohort = _cohort(report, run_failures=[failure])
    assert cohort["planned_anchor_dates"][0] == days[0].isoformat()
    assert cohort["planned_anchor_dates"][1] == days[2].isoformat()
    assert cohort["complete_anchor_count"] == 0
    assert len(cohort["missing_anchor_dates"]) == 20


def test_unresolved_failure_contract_globally_blocks_inference(days):
    report = _build(_report(days), run_failures=[{"run_id": 77, "reason": "unverified"}])
    assert "failed_source_contract_unresolved" in report["adoption_blockers"]
    assert report["cohorts"][0]["statistical_winner_id"] is None


def test_failure_only_cohort_remains_visible_and_missing(days):
    report = _report(days)
    report["cohorts"] = []
    failure = {"rule_version": "failed-v1", "score_spec_hash": "b" * 64, "signal_date": days[0].isoformat()}
    cohort = _cohort(report, run_failures=[failure])
    assert cohort["rule_version"] == "failed-v1"
    assert cohort["complete_anchor_count"] == 0
    assert cohort["missing_anchor_dates"] == [days[0].isoformat()]


def test_pure_pending_anchor_is_counted_without_blocking_mature_evidence(days):
    report = _report(days)
    report["as_of"] = f"{days[38].isoformat()}T16:00:00+08:00"
    report["as_of_completed_date"] = days[38].isoformat()
    cohort = _cohort(report)
    assert cohort["pending_anchor_dates"] == [days[38].isoformat()]
    assert len(cohort["matured_anchor_dates"]) == 19
    assert cohort["missing_anchor_dates"] == []


@pytest.mark.parametrize("horizon", [1, 5, 10, 20])
def test_fixed_nonoverlap_spacing_uses_exchange_sessions_and_h_plus_one(days, horizon):
    report = _report(days, count=4, horizon=horizon)
    cohort = _cohort(report)
    assert cohort["planned_anchor_dates"] == [days[index * (horizon + 1)].isoformat() for index in range(4)]
    assert cohort["complete_anchor_count"] == 4


def test_horizon_one_same_day_exit_is_rejected_as_a_t_plus_one_violation(days):
    report = _report(days)
    _sessions(report)[0]["exit_date"] = _sessions(report)[0]["entry_date"]
    with pytest.raises(ValueError, match="execution dates"):
        _build(report)


def test_cash_is_a_valid_zero_return_with_original_notional_weight(days):
    report = _report(days, values=(0, 0, 0))
    for session in _sessions(report):
        session["selections"] = [_outcome(template, 0, stress=0, drawdown=0) for template in IDS]
    cohort = _cohort(report)
    assert cohort["complete_anchor_count"] == 20
    assert cohort["diagnostic_leader_id"] is cohort["statistical_winner_id"] is None
    assert all(item["two_sided_p_value"] == 1 for item in cohort["comparisons"])


def test_noisy_point_leader_is_not_a_statistical_winner(days):
    report = _report(days, values=(0, 0, 0))
    for index, session in enumerate(_sessions(report)):
        value = .8 if index % 4 >= 2 else -.79
        session["selections"][1] = _outcome(IDS[1], value)
    cohort = _cohort(report)
    assert cohort["diagnostic_leader_id"] == IDS[1]
    assert cohort["statistical_winner_id"] is None


def test_beating_baseline_does_not_establish_superiority_to_the_other_challenger(days):
    cohort = _cohort(_report(days, values=(0, .03, .03)))
    assert cohort["statistical_winner_id"] is None
    assert cohort["diagnostic_leader_id"] is None
    assert sum(item["reject_equal_means"] for item in cohort["comparisons"]) == 2


def test_cost_stress_can_reject_an_apparent_statistical_winner(days):
    report = _report(days)
    for session in _sessions(report):
        session["selections"][1] = _outcome(IDS[1], .03, stress=-.03)
    cohort = _cohort(report)
    assert cohort["statistical_winner_id"] == IDS[1]
    assert cohort["risk_qualified_winner_id"] is None
    assert "cost_stress_advantage_not_confirmed" in cohort["blockers"]


@pytest.mark.parametrize(("drawdown", "qualifies"), [(-.03, True), (-.03001, False)])
def test_single_batch_risk_limit_is_two_percentage_points_with_explicit_boundary(days, drawdown, qualifies):
    report = _report(days)
    _sessions(report)[0]["selections"][1] = _outcome(IDS[1], .03, drawdown=drawdown)
    cohort = _cohort(report)
    assert (cohort["risk_qualified_winner_id"] == IDS[1]) is qualifies
    assert ("independent_batch_drawdown_limit_exceeded" in cohort["blockers"]) is not qualifies


def test_stress_drawdown_is_included_in_the_batch_risk_gate(days):
    report = _report(days)
    _sessions(report)[0]["selections"][1]["stress"]["independent_batch_max_drawdown"] = -.50
    cohort = _cohort(report)
    assert cohort["template_summaries"][1]["worst_independent_batch_drawdown"] == -.50
    assert cohort["risk_qualified_winner_id"] is None


def test_score_contracts_are_never_pooled_into_an_artificial_sample_size(days):
    report = _report(days, count=10)
    other = _report(days, count=10, start=30)["cohorts"][0]
    other["rule_version"] = "test-rule-v2"
    other["score_spec_hash"] = "b" * 64
    for session in other["sessions"]:
        session["run_id"] += 10
    report["cohorts"].append(other)
    result = _build(report)
    assert len(result["cohorts"]) == 2
    assert all(item["statistical_winner_id"] is None for item in result["cohorts"])
    assert "statistical_winner_id" not in result


def test_reordering_sources_does_not_change_date_blocks_or_historical_result(days):
    report = _report(days)
    original = _cohort(report)
    _sessions(report).reverse()
    for session in _sessions(report):
        session["selections"].reverse()
    reordered = _cohort(report)
    assert original == reordered


def test_resampling_seed_is_frozen_before_forward_returns(days):
    report = _report(days)
    original = _cohort(report)
    _sessions(report)[0]["selections"][1] = _outcome(IDS[1], -.30)
    changed = _cohort(report)
    assert changed["contract_digest"] == original["contract_digest"]
    assert changed["comparisons"] != original["comparisons"]
    assert _build(report)["evidence_digest"] != _build(_report(days))["evidence_digest"]


def test_holm_controls_the_fixed_three_test_family_and_does_not_use_fdr():
    comparisons = [{"two_sided_p_value": value} for value in (.02, .03, .04)]
    selection._adjust_holm(comparisons)
    assert [item["holm_adjusted_p_value"] for item in comparisons] == pytest.approx([.06, .06, .06])
    assert not any(item["reject_equal_means"] for item in comparisons)


def test_two_sided_test_has_identical_evidence_for_opposite_effects():
    positive = [.03 + index / 10_000 for index in range(20)]
    assert selection._two_sided_p_value(positive, "fixed") == selection._two_sided_p_value([-value for value in positive], "fixed")


def test_finite_extreme_values_do_not_overflow_means_or_bootstrap(days):
    report = _report(days, values=(0, 1e308, 1e307))
    cohort = _cohort(report)
    assert all(math.isfinite(item["mean_net_return"]) for item in cohort["template_summaries"])
    assert all(math.isfinite(item["mean_net_return_difference"]) for item in cohort["comparisons"])


@pytest.mark.parametrize("value", [True, None, math.nan, math.inf, -math.inf, -1.01, 10 ** 1000])
def test_invalid_net_outcomes_fail_closed(days, value):
    report = _report(days)
    _sessions(report)[0]["selections"][1]["net_return"] = value
    with pytest.raises(ValueError):
        _build(report)


@pytest.mark.parametrize(("field", "value"), [
    ("schema_version", "gross-v1"), ("horizon_sessions", True), ("horizon_sessions", 7),
    ("entry_policy", "D-close"), ("exit_policy", "D+H-close"),
    ("continuous_portfolio_simulation", True), ("promotion_eligible", True),
    ("return_unit", "percentage_points"), ("notional_cash_cny", 0),
    ("as_of", "2026-09-19T16:00:00"), ("as_of_completed_date", "2026-09-17"),
])
def test_wrong_net_contract_is_rejected(days, field, value):
    report = _report(days)
    report[field] = value
    with pytest.raises(ValueError):
        _build(report)


@pytest.mark.parametrize("mutate", [
    lambda report: report["cost_specs"].pop(),
    lambda report: report["cost_specs"][0].update(strategy_fingerprint="wrong"),
    lambda report: report["cohorts"].append(deepcopy(report["cohorts"][0])),
    lambda report: _sessions(report).append(deepcopy(_sessions(report)[0])),
    lambda report: _sessions(report)[1].update(run_id=True),
    lambda report: _sessions(report)[0]["selections"].pop(),
    lambda report: _sessions(report)[0]["selections"][0].update(status="mystery"),
    lambda report: _sessions(report)[0]["selections"][0]["base"].update(status="unavailable"),
    lambda report: _sessions(report)[0]["selections"][0]["base"].update(net_return=.5),
    lambda report: _sessions(report)[0]["selections"][0]["base"].update(independent_batch_max_drawdown=.01),
    lambda report: _sessions(report)[0].update(signal_date="2025-01-04"),
    lambda report: _sessions(report)[0].update(signal_date="2027-01-04"),
    lambda report: report["cohorts"][0].update(rule_version=""),
    lambda report: report["cohorts"][0].update(score_spec_hash="bad"),
    lambda report: report.update(cohorts="not-a-list"),
])
def test_malformed_identities_dates_and_outcome_invariants_are_rejected(days, mutate):
    report = _report(days)
    mutate(report)
    with pytest.raises(ValueError):
        _build(report)


def test_official_execution_evidence_is_required_even_for_plausible_return_numbers(days):
    report = _report(days)
    report["execution_evidence"]["provenance_status"] = "synthetic"
    result = _build(report)
    assert "official_execution_evidence_unavailable" in result["adoption_blockers"]
    assert result["cohorts"][0]["statistical_winner_id"] is None


def test_calendar_failure_is_an_explicit_evidence_gap_and_never_calls_a_provider(days, monkeypatch):
    def unavailable(*args, **kwargs):
        assert kwargs == {"allow_auto_refresh": False}
        raise TradingCalendarCoverageError("calendar unavailable")
    monkeypatch.setattr(selection, "trading_dates_between", unavailable)
    cohort = _cohort(_report(days))
    assert "trusted_calendar_unavailable" in cohort["blockers"]
    assert cohort["planned_anchor_dates"] == []
    assert cohort["statistical_winner_id"] is None


def test_empty_net_report_produces_no_winner(days):
    report = _report(days)
    report["cohorts"] = []
    result = _build(report)
    assert result["status"] == "insufficient_data"
    assert result["cohorts"] == []
    assert result["adoptable_template_id"] is None


def test_capital_budget_is_part_of_the_frozen_contract_identity(days):
    report = _report(days)
    original = _cohort(report)["contract_digest"]
    report["notional_cash_cny"] = 2_000_000
    assert _cohort(report)["contract_digest"] != original


@pytest.mark.parametrize("failures", ["bad", [None], [{1: "invalid key"}]])
def test_source_failure_evidence_requires_bounded_structured_records(days, failures):
    with pytest.raises(ValueError, match="evidence"):
        _build(_report(days), run_failures=failures)


@pytest.mark.parametrize("horizon", [1, 5, 10, 20])
def test_public_selection_contract_matches_report_and_returns_independent_values(days, horizon):
    contract = selection.strategy_template_selection_contract(horizon)
    assert contract == _build(_report(days, count=1, horizon=horizon))["inference_contract"]
    assert contract["anchor_spacing_sessions"] == horizon + 1
    contract["minimum_complete_anchors"] = 1
    assert selection.strategy_template_selection_contract(horizon)["minimum_complete_anchors"] == 20


@pytest.mark.parametrize("horizon", [True, 0, 2, 10.0, None])
def test_public_selection_contract_rejects_unsupported_horizons(horizon):
    with pytest.raises(ValueError, match="horizon"):
        selection.strategy_template_selection_contract(horizon)
