from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.services import prediction_diagnostics as diagnostics
from app.services.prediction_diagnostics import DiagnosticPrediction, FrozenProbabilityBenchmark, build_prediction_diagnostics


DATES = ("2026-01-05", "2026-01-06")


def _benchmark(rows, values=0.5, *, definition="predeclared frozen baseline"):
    probabilities = {row.sample_id: values if isinstance(values, (int, float)) else values[index] for index, row in enumerate(rows)}
    return FrozenProbabilityBenchmark(probabilities, definition, "a" * 64, "2026-01-02")


def _report(rows, *, dates=DATES, benchmarks=None, offset=1):
    return build_prediction_diagnostics(rows, planned_test_dates=dates,
        benchmarks=benchmarks or {"half": _benchmark(rows)}, target_offset_sessions=offset, bootstrap_samples=40)


def test_regime_recognition_cannot_masquerade_as_within_date_stock_ranking():
    rows = [DiagnosticPrediction(f"bull-{index}", DATES[0], int(index < 9), 0.9) for index in range(10)]
    rows += [DiagnosticPrediction(f"bear-{index}", DATES[1], int(index == 9), 0.1) for index in range(10)]
    result = _report(rows)
    assert result["pooled_auc"] == pytest.approx(0.9)
    assert result["cross_sectional_auc"]["date_equal_weight_auc"] == 0.5
    assert result["cross_sectional_auc"]["eligible_session_count"] == 2
    assert result["candidate_selected"] is result["promotion_eligible"] is False


def test_duplicating_one_dates_cross_section_does_not_change_date_equal_scores_or_intervals():
    rows = [DiagnosticPrediction("a", DATES[0], 1, .9), DiagnosticPrediction("b", DATES[0], 0, .9),
            DiagnosticPrediction("c", DATES[1], 1, .1), DiagnosticPrediction("d", DATES[1], 0, .2)]
    repeated = rows[2:] + [replace(row, sample_id=f"{row.sample_id}-{index}") for index in range(12) for row in rows[:2]]
    first, enlarged = _report(rows), _report(repeated)
    assert first["date_balanced_model_scores"] == pytest.approx(enlarged["date_balanced_model_scores"])
    assert first["cross_sectional_auc"]["date_equal_weight_auc"] == enlarged["cross_sectional_auc"]["date_equal_weight_auc"]
    for metric in ("brier_score", "log_loss"):
        assert first["benchmarks"]["half"]["paired_improvement_ci95"][metric] == pytest.approx(enlarged["benchmarks"]["half"]["paired_improvement_ci95"][metric])


def test_candidate_can_beat_constant_half_while_losing_to_an_existing_frozen_benchmark():
    rows = [DiagnosticPrediction(f"{day}-{label}", day, label, .7 if label else .3) for day in DATES for label in (0, 1)]
    benchmarks = {"half": _benchmark(rows), "strong": _benchmark(rows, [.9 if row.outcome else .1 for row in rows])}
    result = _report(rows, benchmarks=benchmarks)
    for metric in ("brier_score", "log_loss"):
        assert result["benchmarks"]["half"]["paired_improvement"][metric] > 0
        assert result["benchmarks"]["strong"]["paired_improvement"][metric] < 0
    assert result["benchmarks"]["half"]["paired_improvement_ci95"]["brier_score"][0] > 0
    assert result["benchmarks"]["strong"]["paired_improvement_ci95"]["brier_score"][1] < 0
    assert result["candidate_selected"] is False


def test_missing_planned_day_is_retained_and_never_compressed_into_a_bootstrap_block(monkeypatch):
    dates = (DATES[0], "2026-01-06", "2026-01-07")
    rows = [DiagnosticPrediction("first", dates[0], 1, .8), DiagnosticPrediction("last", dates[2], 0, .2)]
    monkeypatch.setattr(diagnostics, "date_block_bootstrap_ci", lambda *_args, **_kwargs: pytest.fail("missing calendar reached bootstrap"))
    result = _report(rows, dates=dates)
    assert result["planned_test_dates"] == list(dates)
    assert result["missing_test_dates"] == [dates[1]]
    assert result["benchmarks"]["half"]["paired_improvement_ci95"] is None
    assert "not_compressed" in result["paired_interval_limitation"]
    assert result["cross_sectional_auc"]["single_class_session_count"] == 2
    assert result["cross_sectional_auc"]["missing_session_count"] == 1
    assert result["cross_sectional_auc"]["date_equal_weight_auc"] is None


def test_time_intervals_use_daily_paired_means_full_calendar_and_explicit_offset(monkeypatch):
    dates = tuple((date(2026, 1, 5) + timedelta(days=index)).isoformat() for index in range(10))
    rows = [DiagnosticPrediction(f"{day}-{index}", day, index % 2, .7) for day in dates for index in range(4)]
    calls = []
    def bootstrap(values, seed, samples, *, block_length_sessions):
        calls.append((values, seed, samples, block_length_sessions))
        return [-.1, .1]
    monkeypatch.setattr(diagnostics, "date_block_bootstrap_ci", bootstrap)
    result = _report(rows, dates=dates, offset=5)
    assert len(calls) == 2
    for values, _seed, samples, offset in calls:
        assert [day for day, _value in values] == list(dates)
        assert len(values) == 10 and samples == 40 and offset == 5
    assert result["benchmarks"]["half"]["paired_improvement_ci95"]["block_length_sessions"] == 5


def test_fewer_than_two_offset_blocks_has_no_interval_even_if_all_dates_are_scored():
    rows = [DiagnosticPrediction(str(index), day, index, .7) for index, day in enumerate(DATES)]
    result = _report(rows, offset=2)
    assert result["missing_test_dates"] == []
    assert result["benchmarks"]["half"]["paired_improvement_ci95"] is None
    assert "two_target_offset_blocks" in result["paired_interval_limitation"]


def test_fixed_threshold_subsets_compare_baselines_only_on_the_same_selected_rows():
    rows = [DiagnosticPrediction("selected", DATES[0], 0, .7), DiagnosticPrediction("other", DATES[0], 1, .2),
            DiagnosticPrediction("boundary", DATES[1], 1, .6), DiagnosticPrediction("lower", DATES[1], 0, .59)]
    result = _report(rows)
    lower, upper = result["fixed_selection_subsets"]
    assert lower["threshold"] == .6 and lower["selected_observation_count"] == 2
    assert lower["selected_session_count"] == 2 and lower["observed_positive_rate"] == .5
    assert upper["threshold"] == .7 and upper["selected_observation_count"] == 1
    assert upper["selected_observation_share"] == .25 and upper["mean_probability"] == .7
    assert upper["same_subset_benchmarks"]["half"]["selected_observation_count"] == 1
    assert upper["same_subset_benchmarks"]["half"]["paired_improvement"]["brier_score"] == pytest.approx(.25 - .49)
    assert upper["post_selection_calibration_established"] is upper["promotion_eligible"] is False


def test_empty_predictions_retain_planned_calendar_and_do_not_create_zero_metrics():
    result = _report([])
    assert result["status"] == "no_scored_observations"
    assert result["missing_test_dates"] == list(DATES)
    assert result["pooled_auc"] is result["date_balanced_model_scores"] is None
    assert result["cross_sectional_auc"]["date_equal_weight_auc"] is None
    assert result["fixed_selection_subsets"][0]["observed_positive_rate"] is None


@pytest.mark.parametrize("mutation", ["duplicate_id", "outside_date", "nan_probability", "invalid_label", "missing_baseline", "extra_baseline", "future_fit", "no_provenance"])
def test_invalid_or_misaligned_frozen_inputs_are_rejected(mutation):
    rows = [DiagnosticPrediction("a", DATES[0], 1, .7), DiagnosticPrediction("b", DATES[1], 0, .3)]
    benchmark = _benchmark(rows)
    if mutation == "duplicate_id":
        rows[1] = replace(rows[1], sample_id="a")
    elif mutation == "outside_date":
        rows[1] = replace(rows[1], session_date="2026-01-07")
    elif mutation == "nan_probability":
        rows[1] = replace(rows[1], probability=float("nan"))
    elif mutation == "invalid_label":
        rows[1] = replace(rows[1], outcome=2)
    elif mutation == "missing_baseline":
        benchmark = replace(benchmark, probabilities={"a": .5})
    elif mutation == "extra_baseline":
        benchmark = replace(benchmark, probabilities={"a": .5, "b": .5, "c": .5})
    elif mutation == "future_fit":
        benchmark = replace(benchmark, fitted_through=DATES[0])
    else:
        benchmark = replace(benchmark, provenance_digest="unbound")
    with pytest.raises(ValueError):
        _report(rows, benchmarks={"frozen": benchmark})


@pytest.mark.parametrize("dates,offset", [((), 1), ((DATES[1], DATES[0]), 1), ((DATES[0], DATES[0]), 1), (DATES, 0), (DATES, True)])
def test_missing_or_invalid_planned_calendar_and_offset_fail_closed(dates, offset):
    with pytest.raises(ValueError):
        _report([], dates=dates, offset=offset)


def test_frozen_inputs_are_not_mutated_and_repeated_diagnostics_are_deterministic():
    rows = [DiagnosticPrediction("a", DATES[0], 1, .7), DiagnosticPrediction("b", DATES[1], 0, .3)]
    benchmark = _benchmark(rows)
    before = dict(benchmark.probabilities)
    first = _report(rows, benchmarks={"frozen": benchmark})
    assert _report(list(reversed(rows)), benchmarks={"frozen": benchmark}) == first
    assert benchmark.probabilities == before


def test_bootstrap_work_budget_is_checked_before_computation(monkeypatch):
    dates = tuple((date(2026, 1, 5) + timedelta(days=index)).isoformat() for index in range(2_001))
    monkeypatch.setattr(diagnostics, "date_block_bootstrap_ci", lambda *_args, **_kwargs: pytest.fail("oversized work reached bootstrap"))
    with pytest.raises(ValueError, match="work exceeds"):
        build_prediction_diagnostics([], planned_test_dates=dates, benchmarks={"half": _benchmark([])},
                                     target_offset_sessions=1, bootstrap_samples=2_000)
