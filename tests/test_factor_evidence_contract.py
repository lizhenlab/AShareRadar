from types import SimpleNamespace

import pytest

from app.models.research import CalibrationBucket, FactorCalibration, StandardFactor
from app.services.research_factor_report import assemble_factor_lab_report, build_factor_lab_metrics
from app.services.research_factor_text import _factor_bucket_alpha_text


def _feature(quality=100, signal=100):
    return SimpleNamespace(symbol="600519.SH", updated_at="2026-09-14 15:30:00",
                           data_quality_score=quality, data_quality_level="优秀", signal_confidence=signal)


def _factor(score=80, samples=30, **updates):
    calibration = FactorCalibration(
        sample_count=samples, win_rate=10, avg_forward_5d_return=-10, avg_forward_10d_return=-15,
        max_adverse_return=-20, stability_score=10, expected_level="风险", confidence_level="偏弱",
        participates_in_historical_aggregate=samples > 0, note="合成历史证据",
    )
    values = dict(id="trend_momentum", name="趋势", category="趋势", value="合成",
                  score=score, level="观察", direction="中性", weight=1, calibration=calibration)
    return StandardFactor(**{**values, **updates})


@pytest.mark.parametrize("score", [0, 20, 50, 80, 100])
def test_direction_cannot_change_evidence_sufficiency(score):
    baseline = build_factor_lab_metrics([_factor(score=50)], _feature())
    result = build_factor_lab_metrics([_factor(score=score)], _feature())
    assert result.calibrated_confidence == baseline.calibrated_confidence


@pytest.mark.parametrize("factors", [[], [_factor(samples=0)]])
def test_absent_historical_evidence_cannot_have_sufficiency(factors):
    assert build_factor_lab_metrics(factors, _feature()).calibrated_confidence == 0


def test_one_sample_cannot_produce_high_reliability_even_with_perfect_direction():
    report = assemble_factor_lab_report(_feature(), "常规个股", [], [_factor(score=100, samples=1)])
    assert report.evidence_sufficiency < 35
    assert report.composite_reliability_level == "不足"


def test_quality_bounds_evidence_and_signal_strength_cannot_add_support():
    factors = [_factor()]
    low = build_factor_lab_metrics(factors, _feature(quality=25))
    high_signal = build_factor_lab_metrics(factors, _feature(signal=100))
    low_signal = build_factor_lab_metrics(factors, _feature(signal=0))
    assert low.calibrated_confidence <= 25
    assert high_signal.calibrated_confidence == low_signal.calibrated_confidence


def test_missing_direction_slot_reduces_historical_coverage():
    observed = _factor()
    missing = _factor(id="missing", samples=0, participates_in_current_score=False, data_nature="unavailable")
    complete = build_factor_lab_metrics([observed, _factor(id="second")], _feature())
    incomplete = build_factor_lab_metrics([observed, missing], _feature())
    assert incomplete.calibrated_confidence < complete.calibrated_confidence


def test_more_samples_can_support_evidence_without_requiring_positive_returns():
    sparse = build_factor_lab_metrics([_factor(samples=5)], _feature())
    sufficient = build_factor_lab_metrics([_factor(samples=30)], _feature())
    assert sparse.calibrated_confidence < sufficient.calibrated_confidence
    positive = _factor().model_copy(update={"calibration": _factor().calibration.model_copy(update={
        "win_rate": 90, "avg_forward_5d_return": 10, "avg_forward_10d_return": 15,
        "expected_level": "较强", "stability_score": 90,
    })})
    assert build_factor_lab_metrics([positive], _feature()).calibrated_confidence == sufficient.calibrated_confidence


def _bucket(name, gain):
    return CalibrationBucket(name=name, sample_count=30, win_rate=50,
                             avg_forward_5d_return=gain, avg_forward_10d_return=gain, note="合成场景")


def test_bucket_explanation_cannot_select_the_best_outcome_as_current_context():
    factor = _factor(calibration_buckets=[_bucket("弱趋势", -3), _bucket("强趋势", 8)])
    text = _factor_bucket_alpha_text(factor)
    assert "强趋势" in text and "弱趋势" in text
    assert "8.00%" in text and "-3.00%" in text
    assert "未确认当前场景" in text
    swapped = _factor(calibration_buckets=[_bucket("弱趋势", 8), _bucket("强趋势", -3)])
    assert _factor_bucket_alpha_text(swapped).index("强趋势") < _factor_bucket_alpha_text(swapped).index("弱趋势")
    assert text.index("强趋势") < text.index("弱趋势")


def test_new_evidence_report_discloses_its_limiting_components():
    report = assemble_factor_lab_report(_feature(quality=60), "常规个股", [], [_factor(samples=30)])
    assert report.evidence_sufficiency_version == "factor-evidence-sufficiency.v2"
    assert report.evidence_support.data_quality_score == report.evidence_sufficiency == 60
    assert report.evidence_support.minimum_similar_samples == 30
    assert "方向分、胜率与盈亏不加分" in report.evidence_sufficiency_note
    legacy = report.model_dump(exclude={"evidence_sufficiency_version", "evidence_support"})
    loaded = type(report).model_validate(legacy)
    assert loaded.evidence_sufficiency_version is None and loaded.evidence_support is None


def test_current_factor_cannot_claim_calibration_from_a_different_rule():
    factor = _factor()
    calibration = factor.calibration.model_copy(update={"score_rule_version": "old-rule"})
    with pytest.raises(ValueError, match="scoring rules must match"):
        _factor(score_rule_version="new-rule", calibration=calibration)


def test_composite_observations_do_not_change_evidence_coverage():
    observed = _factor()
    composite = _factor(id="leader", aggregation_role="composite", participates_in_current_score=False)
    base = build_factor_lab_metrics([observed], _feature())
    with_composite = build_factor_lab_metrics([observed, composite], _feature())
    assert base.calibrated_confidence == with_composite.calibrated_confidence
    assert base.evidence_support == with_composite.evidence_support


@pytest.mark.parametrize("samples", [1, 5, 20, 30])
def test_removing_the_weakest_historical_evidence_cannot_increase_sufficiency(samples):
    weak = _factor(id="weak", samples=samples)
    strong = _factor(id="strong", samples=30)
    missing = _factor(id="weak", samples=0, participates_in_current_score=False, data_nature="unavailable")
    before = build_factor_lab_metrics([weak, strong], _feature()).calibrated_confidence
    after = build_factor_lab_metrics([missing, strong], _feature()).calibrated_confidence
    assert after <= before


@pytest.mark.parametrize("current,historical", [("current-price-volume.v2", None), (None, "current-price-volume.v2")])
def test_participating_calibration_cannot_mix_versioned_and_unversioned_scores(current, historical):
    calibration = _factor().calibration.model_copy(update={"score_rule_version": historical})
    with pytest.raises(ValueError, match="scoring rules must match"):
        _factor(score_rule_version=current, calibration=calibration)


def test_v3_report_uses_fixed_groups_and_discloses_actual_shares():
    factors = [_factor(score=80), _factor(id="fund_flow_proxy", score=20)]
    report = assemble_factor_lab_report(_feature(), "高活跃波动股", ["过时的提高权重说明"], factors)
    aggregate = report.score_aggregation
    assert aggregate is not None and aggregate.version == "factor-aggregation.v3"
    assert aggregate.directional_score == report.total_score == 50
    assert aggregate.risk_penalty == 0
    assert aggregate.coverage_pct == pytest.approx(100 / 3)
    assert sum(group.contribution for group in aggregate.groups) == pytest.approx(0)
    assert all(factor.score_share_pct == pytest.approx(100 / 6) for factor in report.factors)
    assert all(factor.weight == pytest.approx(1 / 6) for factor in report.factors)
    assert not any("提高权重" in note for note in [*report.notes, *report.weight_policy])


def test_v3_unknown_factor_cannot_leak_into_alpha_or_historical_support():
    from app.services.research_alpha_points import factor_lab_points

    report = assemble_factor_lab_report(_feature(), "常规个股", [], [_factor(id="unregistered", score=100)])
    assert report.total_score == 50
    assert report.calibration_sample_count == report.evidence_sufficiency == 0
    assert report.factors[0].score_usage == "excluded"
    assert report.factors[0].participates_in_current_score is False
    assert factor_lab_points(report) == []


def test_legacy_report_does_not_invent_v3_aggregation():
    report = assemble_factor_lab_report(_feature(), "常规个股", [], [_factor()])
    payload = report.model_dump(exclude={"score_aggregation"})
    for factor in payload["factors"]:
        factor.pop("score_share_pct")
        factor.pop("score_usage")
    legacy = type(report).model_validate(payload)
    assert legacy.score_aggregation is None
    assert all(factor.score_usage is None and factor.score_share_pct is None for factor in legacy.factors)
