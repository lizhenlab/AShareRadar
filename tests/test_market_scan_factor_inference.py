from __future__ import annotations

import math
import random

import pytest

from app.services import market_scan_evaluation as evaluation
from tests.test_market_scan_evaluation_time_inference import _observation


def _rows():
    rows = []
    for day in range(41):
        for score in (1.0, 2.0, 3.0):
            row = _observation(day, score / 100)
            row.factor_values = {"test_factor": score, "raw_score": 4 - score}
            rows.append(row)
    return rows


@pytest.mark.parametrize("missing", ("factor", "target", "constant", "partial_coverage"))
def test_factor_inference_retains_unavailable_dates_and_refuses_compressed_p_value(missing: str) -> None:
    rows = _rows()
    for row in rows[60:63]:
        if missing == "factor":
            row.factor_values.pop("test_factor")
        elif missing == "target":
            row.returns.clear()
        elif missing == "constant":
            row.factor_values["test_factor"] = 1.0
    if missing == "partial_coverage":
        rows[60].factor_values.pop("test_factor")
    series, partial = evaluation._daily_factor_ics(rows, "test_factor", 5)
    assert len(series) == len(partial) == 41
    assert math.isnan(series[20])
    config = evaluation.EvaluationConfig(bootstrap_samples=100)
    result = evaluation._factor_diagnostic_record("official", "scope", "v5", rows, "test_factor", 5, config)
    assert result["raw_p_value_one_sided"] is None
    assert result["status"] == "insufficient_data"
    assert result["expected_session_count"] == 41
    assert result["independent_session_count"] == 40
    assert result["missing_session_count"] == 1
    assert result["mean_rank_ic"] == 1.0


def test_partial_factor_ic_missing_controls_do_not_disappear_or_contaminate_mean() -> None:
    rows = _rows()
    for day in range(41):
        for index, row in enumerate(rows[day * 3:(day + 1) * 3]):
            row.factor_values["raw_score"] = (1.0, 3.0, 2.0)[index]
    for row in rows[60:63]:
        row.factor_values.pop("raw_score")
    result = evaluation._factor_diagnostic_record(
        "official", "scope", "v5", rows, "test_factor", 5,
        evaluation.EvaluationConfig(bootstrap_samples=100),
    )
    assert result["partial_ic_session_count"] == 40
    assert result["partial_ic_missing_session_count"] == 1
    assert math.isfinite(result["mean_partial_rank_ic_controlling_raw_score"])


def test_factor_inference_uses_real_sample_floor_and_declares_gross_target() -> None:
    rows = _rows()[:3]
    result = evaluation._factor_diagnostic_record(
        "official", "scope", "v5", rows, "test_factor", 5,
        evaluation.EvaluationConfig(minimum_session_count=1, bootstrap_samples=100),
    )
    assert result["status"] == "insufficient_data"
    assert result["raw_p_value_one_sided"] is None
    assert result["rank_ic_target"] == "signal-close-to-D+H-close-gross-return"
    assert result["multiple_testing"]["minimum_independent_session_count"] == 20


def test_factor_inference_is_order_invariant_and_preserves_declared_coverage() -> None:
    rows = _rows()
    rows[60].factor_values.pop("test_factor")
    config = evaluation.EvaluationConfig(complete_day_coverage=0.5, bootstrap_samples=100)
    expected = evaluation._factor_diagnostic_record("official", "scope", "v5", rows, "test_factor", 5, config)
    assert expected["missing_session_count"] == 0
    assert expected["raw_p_value_one_sided"] is not None
    random.Random(37).shuffle(rows)
    actual = evaluation._factor_diagnostic_record("official", "scope", "v5", rows, "test_factor", 5, config)
    assert actual == expected


def test_factor_inference_with_no_valid_dates_returns_null_descriptive_values() -> None:
    rows = _rows()
    for row in rows:
        row.returns.clear()
    result = evaluation._factor_diagnostic_record(
        "official", "scope", "v5", rows, "test_factor", 5,
        evaluation.EvaluationConfig(bootstrap_samples=100),
    )
    assert result["expected_session_count"] == 41
    assert result["mean_rank_ic"] is None
    assert result["mean_partial_rank_ic_controlling_raw_score"] is None
    assert result["raw_p_value_one_sided"] is None


@pytest.mark.parametrize("field", ("factor", "target", "control"))
@pytest.mark.parametrize("value", (math.nan, math.inf, -math.inf))
def test_nonfinite_values_are_excluded_before_coverage_and_ranking(field: str, value: float) -> None:
    rows = _rows()
    for index, row in enumerate(rows):
        row.factor_values["raw_score"] = (1.0, 3.0, 2.0)[index % 3]
    if field == "factor":
        rows[60].factor_values["test_factor"] = value
    elif field == "target":
        rows[60].returns[5] = value
    else:
        rows[60].factor_values["raw_score"] = value
    result = evaluation._factor_diagnostic_record(
        "official", "scope", "v5", rows, "test_factor", 5,
        evaluation.EvaluationConfig(bootstrap_samples=100),
    )
    assert result["partial_ic_missing_session_count"] == 1
    if field == "control":
        assert result["missing_session_count"] == 0
        assert result["raw_p_value_one_sided"] is not None
    else:
        assert result["missing_session_count"] == 1
        assert result["raw_p_value_one_sided"] is None
