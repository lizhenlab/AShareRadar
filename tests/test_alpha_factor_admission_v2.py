"""Alpha cannot re-admit excluded evidence or turn risk safety into direction."""

from types import SimpleNamespace

import pytest

from app.models.analysis import FactorScore
from app.models.research import FactorCalibration, FactorLabReport, StandardFactor
from app.services.research_alpha import _alpha_verdict
from app.services.research_alpha_points import factor_lab_points, overview_factor_points


def _calibration(**updates):
    calibration = FactorCalibration(
        sample_count=30, win_rate=80, avg_forward_5d_return=4, avg_forward_10d_return=6,
        max_adverse_return=-1, stability_score=100, expected_level="较强", confidence_level="较高",
        score_rule_version="alpha-admission-test.v2", note="合成可回放样本",
    )
    # Persisted older responses may predate current model validators.
    return calibration.model_copy(update=updates)


def _factor(*, score=70, **updates):
    factor = StandardFactor(
        id="trend_momentum", name="方向因子", category="趋势", value="合成当前观测", score=score,
        level="积极" if score >= 50 else "风险", direction="正向", weight=1,
        score_rule_version="alpha-admission-test.v2",
    )
    return factor.model_copy(update=updates)


def _report(*factors):
    return FactorLabReport(
        symbol="600519.SH", updated_at="2026-09-15 15:30:00", total_score=50,
        calibrated_confidence=0, factors=[], summary="合成边界审计",
    ).model_copy(update={"factors": list(factors)})


@pytest.mark.parametrize("score", [40, 70])
@pytest.mark.parametrize("updates", [
    {"participates_in_current_score": False},
    {"aggregation_role": "composite", "participates_in_current_score": False},
    {"data_nature": "unavailable", "participates_in_current_score": False},
    {"data_nature": "unavailable"},
])
def test_excluded_factors_cannot_reenter_alpha_from_old_or_current_reports(score, updates):
    factor = _factor(score=score, calibration=_calibration(), **updates)
    assert factor_lab_points(_report(factor)) == []


@pytest.mark.parametrize("updates", [
    {"id": "legacy_unknown"}, {"score_usage": "excluded"}, {"score_usage": "observation"},
])
def test_direct_legacy_report_cannot_bypass_registered_factor_admission(updates):
    assert factor_lab_points(_report(_factor(**updates))) == []


def test_duplicate_factor_identity_cannot_multiply_alpha_evidence():
    first = _factor()
    repeated = first.model_copy(update={"name": "另一个名字的同一因子", "score": 90})
    assert factor_lab_points(_report(first, repeated)) == []


@pytest.mark.parametrize("score,expected", [(90, 0), (72, 0), (40, -5)])
def test_risk_pressure_is_only_a_current_constraint_even_with_positive_history(score, expected):
    factor = _factor(id="risk_pressure", name="风险压力", score=score, calibration=_calibration())
    point = factor_lab_points(_report(factor))[0]
    assert point.impact == expected
    assert point.level != "积极"
    assert "5日胜率" not in point.reason


@pytest.mark.parametrize("role,score,expected", [
    ("risk_constraint", 80, 0), ("risk_constraint", 40, -5), ("direction", 80, 15),
])
def test_overview_constraint_cannot_become_positive_alpha(role, score, expected):
    factor = FactorScore(
        name="概览因子", score=score, level="积极", summary="合成观测", evidence=[], aggregation_role=role,
    )
    insights = SimpleNamespace(overview=SimpleNamespace(factors=[factor]))
    point = overview_factor_points(insights)[0]
    assert point.impact == expected
    if role == "risk_constraint":
        assert point.level != "积极"


@pytest.mark.parametrize("updates", [
    {"score_available": False}, {"participates_in_total_score": False}, {"data_nature": "unavailable"},
])
def test_overview_rejects_each_independent_unavailability_marker(updates):
    factor = FactorScore(name="缺失概览", score=80, level="积极", summary="旧报告", evidence=[]).model_copy(update=updates)
    insights = SimpleNamespace(overview=SimpleNamespace(factors=[factor]))
    assert overview_factor_points(insights) == []


@pytest.mark.parametrize("calibration_updates", [
    {"availability": "execution_evidence_unavailable"},
    {"availability": "execution_evidence_unavailable", "participates_in_historical_aggregate": False},
    {"availability": "insufficient_history", "participates_in_historical_aggregate": False},
    {"availability": "no_similar_samples", "participates_in_historical_aggregate": False},
    {"participates_in_historical_aggregate": False},
    {"score_rule_version": "old-rule.v1", "participates_in_historical_aggregate": False},
    {"score_rule_version": None, "participates_in_historical_aggregate": False},
])
def test_ineligible_calibration_cannot_add_score_or_historical_claims(calibration_updates):
    factor = _factor(calibration=_calibration(**calibration_updates))
    point = factor_lab_points(_report(factor))[0]
    assert point.impact == 10
    assert "5日胜率" not in point.reason


def test_version_mismatch_is_rejected_even_if_old_aggregate_flag_remains_true():
    report = _report(_factor(calibration=_calibration()))
    report.factors[0] = report.factors[0].model_copy(update={"score_rule_version": "different.v1"})
    assert factor_lab_points(report)[0].impact == 10


def test_two_unknown_versions_do_not_prove_matching_history():
    factor = _factor(score_rule_version=None, calibration=_calibration(score_rule_version=None))
    assert factor_lab_points(_report(factor))[0].impact == 10


def test_available_same_rule_history_retains_its_current_positive_adjustment():
    point = factor_lab_points(_report(_factor(calibration=_calibration())))[0]
    assert point.impact > 10
    assert "5日胜率 80.0%" in point.reason


def test_safe_risk_and_excluded_composite_cannot_alone_create_positive_verdict():
    report = _report(
        _factor(id="risk_pressure", name="风险压力", score=72),
        _factor(id="leadership", name="龙头复合", score=90, aggregation_role="composite", participates_in_current_score=False),
    )
    points = factor_lab_points(report)
    positives = [point for point in points if point.impact > 0]
    negatives = [point for point in points if point.impact < 0]
    assert _alpha_verdict(SimpleNamespace(data_quality_score=90), positives, negatives) == "等待确认"
