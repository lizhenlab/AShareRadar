from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services import market_scan_evaluation as evaluation
from app.services.market_scan_evaluation_execution import (
    affordable_execution_purchase, execution_scenario_value, frozen_slot_summary, scenario_net_return,
)
from app.services.paper_trading_costs import resolve_cost_profile
from tests.test_market_scan_evaluation_price_basis import _inputs, _observe


def _row(rank: int, outcome: evaluation._ExecutionOutcome) -> SimpleNamespace:
    return SimpleNamespace(
        rank=rank, raw_score=100-rank, returns={1: 0.1}, execution={1: outcome},
        quote_date="2026-01-05", run_id=1,
    )


def _record(*outcomes: evaluation._ExecutionOutcome, top_n: int = 100) -> dict:
    return evaluation._session_research_record(
        "2026-01-05", [_row(i+1, outcome) for i, outcome in enumerate(outcomes)],
        top_n=top_n, horizon=1, cost_profile=None,
    )


def test_identical_frozen_candidate_and_benchmark_have_zero_net_excess() -> None:
    result = _record(evaluation._ExecutionOutcome("modelled", "test", net_return=0.08))
    assert result["net_excess_return"] == pytest.approx(0)


def test_unfilled_entry_keeps_its_cash_weight_instead_of_reweighting_winners() -> None:
    result = _record(
        evaluation._ExecutionOutcome("modelled", "test", net_return=0.1),
        evaluation._ExecutionOutcome("unfilled", "limit_up_locked", entry_date="2026-01-06"),
    )
    assert result["net_return"] == pytest.approx(0.05)


def test_missing_frozen_slot_does_not_disappear_from_net_return() -> None:
    result = _record(
        evaluation._ExecutionOutcome("modelled", "test", net_return=0.1),
        evaluation._ExecutionOutcome("data_unavailable", "target_bar_missing"),
    )
    assert result["net_return"] is None
    assert result["net_excess_return"] is None


def test_execution_exits_at_fixed_h_plus_one_close_without_delaying() -> None:
    run, result, bars = _inputs()
    observation = _observe(run, result, bars)
    outcome = observation.execution[1]
    assert outcome.status == "modelled"
    assert outcome.exit_date == "2026-01-07"
    assert outcome.price_return == pytest.approx(102/101-1)
    assert outcome.gross_return == pytest.approx((outcome.sell_amount-outcome.buy_amount)/outcome.allocated_capital)
    assert outcome.exit_delay_sessions == 0


def test_blocked_exit_is_unresolved_position_not_cash() -> None:
    run, result, bars = _inputs()
    blocked = (*bars[:2], {**bars[2], "open": 91.8, "close": 91.8, "high": 91.8, "low": 91.8})
    observation = _observe(run, result, blocked)
    assert observation.execution[1].status == "unfilled"
    assert _record(observation.execution[1])["net_return"] is None


def test_cost_stress_uses_same_costed_benchmark() -> None:
    outcome = evaluation._ExecutionOutcome(
        "modelled", "test", net_return=0.08, buy_amount=90_000, sell_amount=100_000,
    )
    rows = [_row(1, outcome), _row(2, replace(outcome, net_return=0.07))]
    for profile in ("base", "stress"):
        result = evaluation._session_research_record(
            "2026-01-05", rows, top_n=2, horizon=1, cost_profile=profile,
        )
        assert result["net_excess_return"] == pytest.approx(0)


def test_missing_middle_session_cannot_supply_stale_previous_close_for_exit() -> None:
    _, _, bars = _inputs()
    sparse = (*bars[:2], {**bars[2], "date": "2026-01-08", "open": 108, "close": 108, "high": 108, "low": 108})
    outcome = evaluation._execution_outcomes(
        symbol="600001.SH", market="SH", list_date="2020-01-02", is_st=False, is_new=False,
        quote_date="2026-01-05", amount=1_300_000_000, bars=sparse,
        eligible_dates=("2026-01-06", "2026-01-07", "2026-01-08"),
        config=evaluation.EvaluationConfig(horizons=(2,)),
    )[2]
    assert outcome.status == "data_unavailable"
    assert outcome.position_open is True
    assert outcome.reason == "holding_path_bar_missing"


def test_declining_price_with_idle_cash_still_has_positive_cost_drag() -> None:
    profile = resolve_cost_profile("base")
    quantity, buy_amount, buy_cost = affordable_execution_purchase(100_000, 600, 100, 100, profile)
    entry = SimpleNamespace(
        quantity=quantity, entry_date="2026-01-06", entry_price=600, buy_amount=buy_amount,
        buy_cost=buy_cost, cost_profile=profile, allocated_capital=100_000, entry_model_limited=False,
    )
    outcome = evaluation._modelled_execution(entry, "2026-01-07", 540, 0, False)
    assert outcome.price_return == pytest.approx(-0.1)
    assert outcome.gross_return == pytest.approx(-0.06)
    assert outcome.cost_drag > 0
    assert outcome.net_return < outcome.gross_return


def test_stray_non_session_bar_cannot_override_exact_exit_limit_reference() -> None:
    _, _, bars = _inputs()
    dated = (
        {**bars[0], "date": "2026-01-08"},
        {**bars[1], "date": "2026-01-09", "open": 100, "close": 100, "high": 101, "low": 99},
        {**bars[1], "date": "2026-01-10", "open": 120, "close": 120, "high": 120, "low": 120},
        {**bars[2], "date": "2026-01-12", "open": 108, "close": 108, "high": 108, "low": 108},
    )
    outcome = evaluation._execution_outcomes(
        symbol="600001.SH", market="SH", list_date="2020-01-02", is_st=False, is_new=False,
        quote_date="2026-01-08", amount=1_300_000_000, bars=dated,
        eligible_dates=("2026-01-09", "2026-01-12"), config=evaluation.EvaluationConfig(horizons=(1,)),
    )[1]
    assert outcome.status == "modelled"
    assert outcome.price_return == pytest.approx(0.08)


@pytest.mark.parametrize("coverage,passed", [(0.5, True), (0.95, False)])
def test_configured_coverage_gate_does_not_impute_unresolved_slots(coverage: float, passed: bool) -> None:
    result = frozen_slot_summary(
        [evaluation._ExecutionOutcome("modelled", "test", net_return=0.1), None],
        minimum_coverage=coverage,
    )
    assert result["outcome_coverage"] == 0.5
    assert result["coverage_gate_passed"] is passed
    assert result["net_return"] is None
    assert result["conditional_known_slot_return"] == 0.1
    assert result["status_counts"] == {"data_unavailable": 1, "modelled": 1}


@pytest.mark.parametrize("outcome", [
    None,
    evaluation._ExecutionOutcome("modelled", "nan", net_return=float("nan")),
    evaluation._ExecutionOutcome("modelled", "infinite", net_return=float("inf")),
    evaluation._ExecutionOutcome("unfilled", "exit_not_sellable_within_delay"),
])
def test_unknown_or_nonfinite_returns_cannot_become_cash(outcome) -> None:
    assert execution_scenario_value(outcome) is None


def test_purchase_includes_costs_and_respects_minimum_lot() -> None:
    profile = resolve_cost_profile("base")
    quantity, gross, cost = affordable_execution_purchase(100_000, 100, 100, 100, profile)
    assert quantity == 900
    assert gross + cost <= 100_000
    assert affordable_execution_purchase(10_000, 100, 100, 100, profile) == (0, 0, 0)


def test_stress_scenario_rejects_unfunded_purchase_and_missing_amounts() -> None:
    assert scenario_net_return(evaluation._ExecutionOutcome("modelled", "missing"), "stress") is None
    outcome = evaluation._ExecutionOutcome("modelled", "overbudget", buy_amount=100_000, sell_amount=110_000, allocated_capital=100_000)
    assert scenario_net_return(outcome, "stress") is None


def test_cohort_net_excess_uses_executable_benchmark_and_keeps_missing_sessions() -> None:
    known = _row(1, evaluation._ExecutionOutcome("modelled", "test", net_return=0.1))
    summary = evaluation._net_execution_statistics([known], [known], 1, 0.95)
    assert summary["average_net_excess_return"] == pytest.approx(0)
    missing = _row(2, evaluation._ExecutionOutcome("data_unavailable", "missing"))
    missing.run_id = 2
    summary = evaluation._net_execution_statistics([known, missing], [known, missing], 1, 0.95)
    assert summary["average_net_return"] is None
    assert summary["average_net_excess_return"] is None
    assert summary["expected_session_count"] == 2


def test_requested_variants_do_not_prove_preregistration() -> None:
    criterion = evaluation._base_promotion_criteria({})["preregistered_trial_family"]
    assert criterion["passed"] is False
    assert criterion["observed"] == "unverified"
