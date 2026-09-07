from __future__ import annotations

from datetime import date, timedelta
import random
from typing import Any, cast
from types import SimpleNamespace

import pytest

from app.services import market_scan_evaluation as evaluation
from app.services.market_scan_evaluation_statistics import (
    moving_block_bootstrap_confidence_interval,
    moving_block_bootstrap_p_value,
    synthetic_return_chain_maximum_drawdown,
)


def _observation(index: int, value: float, *, horizon: int = 5) -> Any:
    return SimpleNamespace(
        run_id=1000 - index,
        quote_date=(date(2026, 1, 1) + timedelta(days=index)).isoformat(),
        mode="official",
        scope="沪市 + 深市 + 北交所当前上市A股",
        rule_version="time-inference-test",
        raw_score=float(index),
        rank=1,
        returns={horizon: value},
        adverse={},
        execution={horizon: evaluation._ExecutionOutcome("modelled", "fixed-exit", net_return=value)},
    )


@pytest.mark.parametrize("values", ([0.123], [0.1, -0.2], [0.01] * 19))
def test_small_samples_do_not_emit_inference_intervals(values: list[float]) -> None:
    assert evaluation._cluster_bootstrap_ci(values, "single-session", 200) is None


def test_nonfinite_session_is_not_silently_deleted_from_the_time_axis() -> None:
    assert evaluation._cluster_bootstrap_ci([0.1] * 20 + [float("nan")], "gap", 200) is None


def test_horizon_returns_cannot_claim_shared_capital_portfolio_drawdown() -> None:
    rows = tuple(_observation(index, value) for index, value in enumerate((0.10, -0.20, 0.10)))
    result = evaluation._return_statistics(
        {"mode": "official"}, rows, rows,
        [(row, row.returns[5]) for row in rows], 1, 5,
        evaluation.EvaluationConfig(horizons=(5,), top_sizes=(1,), bootstrap_samples=100),
    )

    assert result["session_maximum_drawdown"] is None
    drawdown = cast(dict[str, object], result["portfolio_drawdown"])
    assert drawdown["status"] == "unavailable"
    assert drawdown["eligible_for_promotion"] is False
    assert drawdown["missing_evidence"] == [
        "shared_capital_allocation_policy",
        "dated_cash_positions_and_transaction_costs",
        "daily_valuation_for_open_and_blocked_positions",
    ]
    diagnostic = cast(dict[str, object], result["horizon_return_chain_diagnostic"])
    assert diagnostic["eligible_for_promotion"] is False
    assert diagnostic["maximum_drawdown"] == pytest.approx(-0.2)


@pytest.mark.parametrize("horizon,minimum", ((1, 20), (5, 20), (20, 40)))
def test_inference_requires_date_floor_and_two_horizon_blocks(horizon: int, minimum: int) -> None:
    assert evaluation._cluster_bootstrap_ci(
        [0.01] * (minimum - 1), "floor", 100, block_length=horizon, minimum_count=1,
    ) is None
    assert evaluation._cluster_bootstrap_ci(
        [0.01] * minimum, "floor", 100, block_length=horizon, minimum_count=1,
    ) == pytest.approx([0.01, 0.01])
    assert evaluation._inference_contract(horizon, 1)["minimum_session_count"] == minimum


def test_longer_declared_sample_floor_cannot_be_lowered_by_default() -> None:
    assert evaluation._cluster_bootstrap_ci(
        [0.01] * 40, "floor", 100, block_length=5, minimum_count=60,
    ) is None


def test_persistent_series_has_wider_block_interval_than_independent_resampling() -> None:
    values = [-0.1] * 30 + [0.1] * 30
    arguments = {"samples": 1000, "seed_text": "persistent-dates", "minimum_count": 20}
    interval = moving_block_bootstrap_confidence_interval(values, block_length=5, **arguments)
    replayed = moving_block_bootstrap_confidence_interval(values, block_length=5, **arguments)
    independent = moving_block_bootstrap_confidence_interval(values, block_length=1, **arguments)

    assert interval is not None and independent is not None
    assert interval == replayed
    assert interval[1] - interval[0] > 1.5 * (independent[1] - independent[0])


@pytest.mark.parametrize(
    "options",
    (
        {"samples": 99}, {"samples": True}, {"block_length": 0},
        {"block_length": 1.5}, {"minimum_count": 0}, {"block_length": 11},
    ),
)
def test_invalid_or_underpowered_block_contract_produces_no_inference(options: dict[str, Any]) -> None:
    arguments = {"samples": 100, "block_length": 5, "minimum_count": 20, "seed_text": "invalid"} | options
    assert moving_block_bootstrap_confidence_interval([0.01] * 20, **arguments) is None
    assert moving_block_bootstrap_p_value([0.01] * 20, **arguments) is None


@pytest.mark.parametrize("invalid", (float("nan"), float("inf"), -float("inf")))
def test_p_value_does_not_delete_nonfinite_dates(invalid: float) -> None:
    assert moving_block_bootstrap_p_value(
        [0.01] * 40 + [invalid], samples=100, block_length=5,
        minimum_count=20, seed_text="nonfinite",
    ) is None


def test_return_inference_uses_date_order_instead_of_run_ids_or_input_order() -> None:
    rows = [_observation(index, -0.1 if index < 30 else 0.1) for index in range(60)]
    config = evaluation.EvaluationConfig(horizons=(5,), top_sizes=(1,), bootstrap_samples=200)

    def statistics() -> dict[str, object]:
        return evaluation._return_statistics(
            {"mode": "official"}, tuple(rows), tuple(rows),
            [(row, row.returns[5]) for row in rows], 1, 5, config,
        )

    ordered = statistics()
    random.Random(24).shuffle(rows)
    permuted = statistics()
    for key in (
        "session_return_confidence_interval_95", "session_excess_confidence_interval_95",
        "horizon_return_chain_diagnostic",
    ):
        assert ordered[key] == permuted[key]
    contract = cast(dict[str, object], ordered["confidence_interval_inference"])
    assert contract["block_length_sessions"] == 5


@pytest.mark.parametrize("horizon", (1, 5, 20))
def test_rank_ic_interval_respects_date_order_and_label_horizon(horizon: int) -> None:
    rows = []
    for index in range(60):
        for score in (1.0, 2.0, 3.0):
            row = _observation(index, score / 100 if index < 30 else -score / 100, horizon=horizon)
            row.raw_score = score
            rows.append(row)
    config = evaluation.EvaluationConfig(horizons=(horizon,), top_sizes=(1,), bootstrap_samples=200)
    ordered = evaluation._rank_ic_metrics(tuple(rows), config)
    random.Random(24).shuffle(rows)
    assert evaluation._rank_ic_metrics(tuple(rows), config) == ordered
    assert ordered[0]["confidence_interval_95"] is not None
    contract = cast(dict[str, object], ordered[0]["confidence_interval_inference"])
    assert contract["block_length_sessions"] == horizon + 1


@pytest.mark.parametrize("values", ([], [float("nan")], [float("inf")], [-1.01], [1e308] * 3))
def test_chain_diagnostic_rejects_invalid_wealth(values: list[float]) -> None:
    assert synthetic_return_chain_maximum_drawdown(values) is None


def test_chain_diagnostic_handles_complete_loss_and_never_recovers_from_zero() -> None:
    assert synthetic_return_chain_maximum_drawdown([0.1, -1.0, 10.0]) == -1.0


def test_missing_portfolio_nav_blocks_drawdown_gate_despite_positive_chain_diagnostic() -> None:
    criteria = evaluation._contract_promotion_criteria(
        {},
        {
            "independent_session_count": 100,
            "session_maximum_drawdown": None,
            "horizon_return_chain_diagnostic": {"maximum_drawdown": 0.0},
        },
        {"mode": "official", "scope": "沪市 + 深市 + 北交所当前上市A股", "rule_version": "v5"},
        40,
    )
    assert criteria["maximum_drawdown_5d"]["passed"] is False


def test_factor_test_series_is_ordered_by_date_before_block_resampling() -> None:
    rows = []
    for index in range(40):
        for score in (1.0, 2.0, 3.0):
            row = _observation(index, score if index < 20 else -score)
            row.factor_values = {"test_factor": score, "raw_score": score}
            rows.append(row)
    ordered = evaluation._daily_factor_ics(rows, "test_factor", 5)
    random.Random(24).shuffle(rows)
    assert evaluation._daily_factor_ics(rows, "test_factor", 5) == ordered


def test_paired_net_return_test_block_covers_entry_and_fixed_exit_span(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def p_value(values: object, **options: object) -> float:
        captured.update(options)
        return 0.5

    monkeypatch.setattr(evaluation, "moving_block_bootstrap_p_value", p_value)
    evaluation._candidate_test_record("v5_4_skip5_multilevel_residual", {}, {}, 40)
    assert captured["block_length"] == 6
    payload = evaluation._candidate_multiple_testing_payload(
        {}, candidate_count=1, available_count=0, minimum_sessions=40, status="insufficient_data",
    )
    contract = cast(dict[str, object], payload["session_resampling"])
    assert contract["block_length_sessions"] == 6


@pytest.mark.parametrize("gap_side", ("candidate_null", "candidate_absent", "production_absent", "both_null"))
def test_paired_test_does_not_drop_unresolved_target_dates(gap_side: str) -> None:
    dates = [(date(2026, 1, 1) + timedelta(days=index)).isoformat() for index in range(41)]
    production = dict.fromkeys(dates, 0.0)
    sessions = [{"quote_date": session, "net_excess_return": 0.1} for session in dates]
    missing_date = dates[20]
    if gap_side in {"candidate_null", "both_null"}:
        sessions[20]["net_excess_return"] = None
    elif gap_side == "candidate_absent":
        sessions.pop(20)
    if gap_side in {"production_absent", "both_null"}:
        del production[missing_date]
    record, p_value = evaluation._candidate_test_record(
        "v5_4_skip5_multilevel_residual", {"promotion_evidence": {"sessions": sessions}}, production, 40,
    )
    assert p_value is None
    assert record["status"] == "insufficient_data"
    assert record["paired_independent_session_count"] == 40
    assert record["expected_paired_session_count"] == 41
    assert record["missing_paired_session_count"] == 1
    assert "incomplete_paired_target_dates" in record["insufficient_reasons"]


def _rank_rows_with_execution() -> list[Any]:
    rows = []
    for index in range(41):
        for score in (1.0, 2.0, 3.0):
            row = _observation(index, score / 100)
            row.raw_score = score
            row.execution[5] = evaluation._ExecutionOutcome("modelled", "fixed-exit", net_return=-score / 100)
            rows.append(row)
    return rows


def test_rank_ic_uses_fixed_executable_target_instead_of_gross_quote_return() -> None:
    rows = _rank_rows_with_execution()
    result = evaluation._rank_ic_metrics(tuple(rows), evaluation.EvaluationConfig(horizons=(5,)))[0]
    assert result["mean_rank_ic"] == pytest.approx(-1.0)
    assert result["rank_ic_target"] == "fixed-horizon-executable-net-slot-return"
    assert result["confidence_interval_95"] == pytest.approx([-1.0, -1.0])


def test_rank_ic_preserves_unresolved_date_in_ci_even_with_enough_valid_dates() -> None:
    rows = _rank_rows_with_execution()
    rows[60].execution.clear()
    result = evaluation._rank_ic_metrics(tuple(rows), evaluation.EvaluationConfig(horizons=(5,)))[0]
    assert result["confidence_interval_95"] is None
    assert result["independent_session_count"] == 40
    assert result["expected_session_count"] == 41
    assert result["missing_session_count"] == 1
    assert "incomplete_rank_ic_target_dates" in result["insufficient_reasons"]


def test_rank_ic_uses_configured_cross_section_outcome_coverage() -> None:
    rows = _rank_rows_with_execution()
    for index in range(0, len(rows), 3):
        rows[index].execution.clear()
    strict = evaluation._rank_ic_metrics(tuple(rows), evaluation.EvaluationConfig(horizons=(5,), complete_day_coverage=0.95))[0]
    allowed = evaluation._rank_ic_metrics(tuple(rows), evaluation.EvaluationConfig(horizons=(5,), complete_day_coverage=0.6))[0]
    assert strict["confidence_interval_95"] is None
    assert strict["independent_session_count"] == 0
    assert allowed["confidence_interval_95"] == pytest.approx([-1.0, -1.0])


def test_paired_test_preserves_production_only_unfinished_date() -> None:
    dates = [(date(2026, 1, 1) + timedelta(days=index)).isoformat() for index in range(41)]
    production_sessions = [{"quote_date": session, "net_excess_return": 0.0} for session in dates]
    production_sessions[-1]["net_excess_return"] = None
    candidate_sessions = [{"quote_date": session, "net_excess_return": 0.1} for session in dates[:-1]]
    result = evaluation._candidate_multiple_testing_control(
        {"promotion_evidence": {"sessions": production_sessions}},
        {"v5_4_skip5_multilevel_residual": {"promotion_evidence": {"sessions": candidate_sessions}}},
        minimum_sessions=40,
    )
    candidates = cast(dict[str, Any], result["candidate_results"])
    record = candidates["v5_4_skip5_multilevel_residual"]
    assert record["raw_p_value_one_sided"] is None
    assert record["expected_paired_session_count"] == 41
    assert record["missing_paired_session_count"] == 1


@pytest.mark.parametrize("gap", ("both", "selected", "benchmark"))
def test_gross_intervals_preserve_complete_frozen_date_axis(gap: str) -> None:
    selected = tuple(_observation(index, 0.1) for index in range(21))
    benchmark = tuple(_observation(index, 0.05) for index in range(21))
    if gap in {"both", "selected"}:
        selected[10].returns.clear()
    if gap in {"both", "benchmark"}:
        benchmark[10].returns.clear()
    metric = evaluation._return_statistics(
        {"mode": "official"}, selected, benchmark,
        [(row, row.returns[5]) for row in selected if 5 in row.returns], 1, 5,
        evaluation.EvaluationConfig(horizons=(5,), top_sizes=(1,), bootstrap_samples=100),
    )
    assert metric["session_excess_confidence_interval_95"] is None
    if gap in {"both", "selected"}:
        assert metric["session_return_confidence_interval_95"] is None
    else:
        assert metric["session_return_confidence_interval_95"] == pytest.approx([0.1, 0.1])
    contract = cast(dict[str, object], metric["confidence_interval_inference"])
    assert contract["target"] == "signal-close-to-D+H-close-gross-return"
    assert contract["eligible_for_promotion"] is False
    assert contract["expected_session_count"] == 21
    assert contract["excess_valid_session_count"] == 20
    assert contract["excess_missing_session_count"] == 1
    assert contract["return_missing_session_count"] == (0 if gap == "benchmark" else 1)


def test_gross_intervals_preserve_benchmark_only_date() -> None:
    selected = tuple(_observation(index, 0.1) for index in range(20))
    benchmark = tuple(_observation(index, 0.05) for index in range(21))
    metric = evaluation._return_statistics(
        {"mode": "official"}, selected, benchmark,
        [(row, row.returns[5]) for row in selected], 1, 5,
        evaluation.EvaluationConfig(horizons=(5,), top_sizes=(1,), bootstrap_samples=100),
    )
    assert metric["session_return_confidence_interval_95"] is None
    assert metric["session_excess_confidence_interval_95"] is None
    contract = cast(dict[str, object], metric["confidence_interval_inference"])
    assert contract["expected_session_count"] == 21
    assert contract["return_missing_session_count"] == 1


def test_regime_counts_expected_valid_and_missing_target_dates() -> None:
    rows = _rank_rows_with_execution()
    for row in rows:
        row.regime = "strong"
    rows[60].execution.clear()
    result = evaluation._regime_robustness_slice(rows, "strong", evaluation.EvaluationConfig(horizons=(5,)))
    assert result["status"] == "insufficient_data"
    assert result["independent_session_count"] == 40
    assert result["expected_session_count"] == 41
    assert result["missing_session_count"] == 1
    assert result["mean_rank_ic"] is None
    assert result["mean_net_excess_return"] is None
