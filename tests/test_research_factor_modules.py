from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.models.schemas import FactorCalibration, StandardFactor
from app.services import research_factors
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_chip import build_chip_analysis
from app.services.research_factor_current import build_current_factors
from app.services.research_factor_report import (
    _effective_calibration_sample_count,
    assemble_factor_lab_report,
    build_factor_lab_metrics,
    factor_risk_count,
    factor_support_count,
)
from app.services.research_factor_specs import _factor_specs
from app.services.research_factor_text import _factor_confirmation_text, _factor_score_impact
from app.services.research_features import build_feature_snapshot, build_leadership_report
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote


def test_research_factors_facade_preserves_legacy_helpers() -> None:
    assert research_factors.build_current_factors is build_current_factors
    assert research_factors._factor_specs is _factor_specs
    assert research_factors._factor_score_impact is _factor_score_impact


def test_research_factor_report_module_exposes_report_assembly_helpers() -> None:
    assert callable(build_factor_lab_metrics)
    assert callable(assemble_factor_lab_report)


def test_factor_lab_uses_minimum_per_factor_samples_instead_of_summing_six_by_four() -> None:
    factors = [_factor(index, sample_count=4) for index in range(6)]

    report = assemble_factor_lab_report(_feature(), "常规个股", [], factors)

    assert _effective_calibration_sample_count(factors) == 4
    assert report.calibration_sample_count == 4
    assert "最低单因子有效样本只有 4 个" in _factor_confirmation_text(report)
    assert any("最低单因子相似样本数计为 4 个" in note and "不跨因子累加" in note for note in report.notes)


def test_factor_lab_preserves_a_single_factors_full_sample_support() -> None:
    factors = [_factor(1, sample_count=30), _uncalibrated_valuation_factor()]

    report = assemble_factor_lab_report(_feature(), "常规个股", [], factors)

    assert _effective_calibration_sample_count(factors) == 30
    assert report.calibration_sample_count == 30
    assert "最低单因子有效样本 30 个" in _factor_confirmation_text(report)
    assert "综合证据充分度" in _factor_confirmation_text(report)
    assert "/100" in _factor_confirmation_text(report)
    assert "置信度" not in _factor_confirmation_text(report)
    assert any("未校准项：估值锚" in note for note in report.notes)


def test_factor_lab_stays_at_zero_when_all_calibration_factors_have_no_samples() -> None:
    factors = [
        *[_factor(index, sample_count=0) for index in range(6)],
        _uncalibrated_valuation_factor(),
    ]

    metrics = build_factor_lab_metrics(factors, _feature())
    report = assemble_factor_lab_report(_feature(), "常规个股", [], factors)

    assert _effective_calibration_sample_count(factors) == 0
    assert metrics.calibration_sample_count == 0
    assert metrics.calibration_factor_count == 6
    assert metrics.uncalibrated_factor_names == ("估值锚",)
    assert report.calibration_sample_count == 0


def test_historical_aggregate_participation_is_independent_from_confidence_label() -> None:
    included = _factor(1, sample_count=12, confidence_level="中等", participates_in_historical_aggregate=True)
    excluded = _factor(2, sample_count=3, confidence_level="中等", participates_in_historical_aggregate=False)

    metrics = build_factor_lab_metrics([included, excluded], _feature())

    assert included.calibration and excluded.calibration
    assert included.calibration.confidence_level == excluded.calibration.confidence_level
    assert _effective_calibration_sample_count([included, excluded]) == 12
    assert metrics.calibration_factor_count == 1
    assert metrics.uncalibrated_factor_names == (excluded.name,)


def test_excluded_factor_stability_cannot_change_historical_evidence_aggregates() -> None:
    included = _factor(1, sample_count=12, name="参与因子")
    excluded_positive = _factor(
        2,
        sample_count=12,
        name="排除正向因子",
        score=90,
        stability_score=0,
        participates_in_historical_aggregate=False,
    )
    excluded_risk = _factor(
        3,
        sample_count=12,
        name="排除风险因子",
        score=20,
        expected_level="风险",
        participates_in_historical_aggregate=False,
    )
    high_stability_excluded = excluded_positive.model_copy(
        update={
            "calibration": excluded_positive.calibration.model_copy(update={"stability_score": 100})
            if excluded_positive.calibration
            else None
        }
    )

    low_stability = build_factor_lab_metrics([included, excluded_positive, excluded_risk], _feature())
    high_stability = build_factor_lab_metrics([included, high_stability_excluded, excluded_risk], _feature())

    assert low_stability.total_score == high_stability.total_score
    assert low_stability.calibrated_confidence == high_stability.calibrated_confidence
    assert low_stability.positives == high_stability.positives == [included.name]
    assert low_stability.negatives == high_stability.negatives == []
    assert factor_support_count([included, excluded_positive]) == factor_support_count([included]) == 1
    assert factor_risk_count([included, excluded_risk]) == factor_risk_count([included]) == 0


def test_real_current_factors_keep_valuation_scored_without_blocking_six_factor_samples() -> None:
    analysis, insights, feature, chip, leadership = _fully_calibrated_factor_inputs()

    factors = build_current_factors(analysis, insights, feature, chip, leadership)
    metrics = build_factor_lab_metrics(factors, feature)
    report = assemble_factor_lab_report(feature, "常规个股", [], factors)

    assert [factor.id for factor in factors] == [
        "trend_momentum",
        "volume_confirmation",
        "risk_pressure",
        "fund_flow_proxy",
        "chip_position",
        "leadership_strength",
        "valuation_anchor",
    ]
    calibration_samples = [factor.calibration.sample_count for factor in factors[:6] if factor.calibration]
    valuation = factors[-1]
    trend = factors[0]
    volume = factors[1]
    flow_proxy = factors[3]
    assert f"趋势信号可靠度 {feature.signal_confidence}/100" in " ".join(trend.evidence)
    assert all("趋势信号置信度" not in item for item in trend.evidence)
    assert any(f"信号可靠度 {feature.signal_confidence}/100" in note for note in feature.notes)
    assert all("信号可信度" not in note for note in feature.notes)
    assert "涨跌幅" in volume.value and "%" in volume.value
    assert flow_proxy.name == "量价连续性（衍生）"
    assert flow_proxy.category == "量价衍生"
    assert flow_proxy.data_nature == "derived"
    assert flow_proxy.methodology and "不是真实资金流" in flow_proxy.methodology
    assert "量价热度评分（衍生）" in flow_proxy.value
    assert all("资金评分" not in item and "资金源" not in item for item in flow_proxy.evidence)
    assert len(calibration_samples) == 6
    assert min(calibration_samples) >= 24
    assert valuation.calibration is not None
    assert valuation.calibration.sample_count == 0
    assert valuation.calibration.confidence_level == "待补数据"
    assert valuation.calibration.participates_in_historical_aggregate is False
    assert metrics.scoring_factor_count == 7
    assert metrics.calibration_factor_count == 6
    assert metrics.uncalibrated_factor_names == ("估值锚",)
    assert report.calibration_sample_count == min(calibration_samples)
    assert report.evidence_sufficiency == report.calibrated_confidence
    assert report.composite_reliability_level in {"较高", "中等", "较低", "不足"}
    assert all("低置信" not in note for note in report.notes)
    assert any(
        "7 个因子参与评分" in note
        and "6 个参与历史校准" in note
        and "未校准项：估值锚" in note
        for note in report.notes
    )


def _feature():
    return SimpleNamespace(
        symbol="600000.SH",
        updated_at="2026-07-10T10:00:00",
        signal_confidence=90,
        data_quality_score=90,
        data_quality_level="优秀",
    )


def _factor(
    index: int,
    *,
    sample_count: int,
    factor_id: str | None = None,
    name: str | None = None,
    confidence_level: str | None = None,
    score: int = 70,
    stability_score: int = 80,
    expected_level: str = "较强",
    participates_in_historical_aggregate: bool = True,
) -> StandardFactor:
    return StandardFactor(
        id=factor_id or f"factor_{index}",
        name=name or f"测试因子{index}",
        category="测试",
        value="偏强",
        score=score,
        level="偏强",
        direction="正向",
        weight=1.0,
        calibration=FactorCalibration(
            sample_count=sample_count,
            win_rate=65,
            avg_forward_5d_return=1.2,
            avg_forward_10d_return=1.8,
            max_adverse_return=-2.0,
            stability_score=stability_score,
            expected_level=expected_level,
            confidence_level=confidence_level or ("较高" if sample_count >= 12 else "偏低"),
            participates_in_historical_aggregate=participates_in_historical_aggregate,
            note="测试校准",
        ),
    )


def _uncalibrated_valuation_factor() -> StandardFactor:
    return _factor(
        6,
        sample_count=0,
        factor_id="valuation_anchor",
        name="估值锚",
        confidence_level="待补数据",
        stability_score=0,
        expected_level="待补数据",
        participates_in_historical_aggregate=False,
    )


def _fully_calibrated_factor_inputs():
    start = date(2026, 1, 1)
    klines = []
    for index in range(100):
        close = 100 + index * 0.5
        row = make_kline(
            date=(start + timedelta(days=index)).isoformat(),
            close=close,
            high=close + 1,
            low=close - 1,
            volume=1600 + index * 20,
        )
        if index % 5 in {3, 4}:
            row = row.model_copy(update={"open": close + 0.5})
        klines.append(row)
    price = klines[-1].close
    previous_close = klines[-2].close
    quote = make_quote(
        price=price,
        prev_close=previous_close,
        high=price + 1,
        low=price - 1,
        change_pct=(price / previous_close - 1) * 100,
        turnover_rate=4.2,
    ).model_copy(update={"open": price - 0.2})
    quality = build_data_quality(
        quote,
        klines,
        now=datetime(2026, 5, 13, 16, 0, 0),
    ).model_copy(update={"score": 90, "level": "优秀", "anomalies": []})
    analysis = build_analysis(quote, klines, data_quality=quality)
    insights = build_stock_insight_bundle(analysis)
    feature = build_feature_snapshot(analysis, insights)
    chip = build_chip_analysis(analysis, feature)
    leadership = build_leadership_report(analysis, insights, feature)
    return analysis, insights, feature, chip, leadership
