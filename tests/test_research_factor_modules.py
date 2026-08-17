from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

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
from app.services.research_factor_scoring import _factor_calibration_quality, _weighted_factor_score
from app.services.research_factor_specs import _factor_specs
from app.services.research_factor_text import (
    _factor_calibration_impact,
    _factor_confirmation_text,
    _factor_score_impact,
)
from app.services.research_features import build_feature_snapshot, build_leadership_report
from app.services.stock_insights import build_stock_insight_bundle
from app.services.trading_calendar import next_trade_dates
from tests.factories import make_kline, make_quote


def test_research_factors_facade_preserves_legacy_helpers() -> None:
    assert research_factors.build_current_factors is build_current_factors
    assert research_factors._factor_specs is _factor_specs
    assert research_factors._factor_score_impact is _factor_score_impact


def test_research_factor_report_module_exposes_report_assembly_helpers() -> None:
    assert callable(build_factor_lab_metrics)
    assert callable(assemble_factor_lab_report)


@pytest.mark.parametrize(
    "updates",
    [
        {"sample_count": -1},
        {"win_rate": 200},
        {"avg_forward_5d_return": float("inf")},
        {"stability_score": 999},
        {
            "availability": "execution_evidence_unavailable",
            "participates_in_historical_aggregate": True,
            "unavailable_reason": None,
        },
        {"sample_count": 0, "participates_in_historical_aggregate": True},
    ],
)
def test_factor_calibration_rejects_impossible_availability_and_metrics(updates: dict) -> None:
    payload = {
        "sample_count": 10,
        "win_rate": 55,
        "avg_forward_5d_return": 1.0,
        "avg_forward_10d_return": 1.5,
        "max_adverse_return": -2.0,
        "stability_score": 60,
        "confidence_level": "中等",
        "note": "测试",
    }
    payload.update(updates)

    with pytest.raises(ValidationError):
        FactorCalibration.model_validate(payload)


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
    assert metrics.calibration_factor_count == 0
    assert len(metrics.uncalibrated_factor_names) == 7
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
    included = _factor(1, sample_count=20, name="参与因子")
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


def test_small_positive_calibration_cannot_inflate_scores_or_confidence() -> None:
    small = _factor(1, sample_count=12, score=70, stability_score=100, expected_level="较强")
    base_impact = round((small.score - 50) / 2)

    assert _factor_score_impact(small) == base_impact
    assert small.calibration is not None
    assert _factor_calibration_impact(small.calibration) == 0
    assert _factor_calibration_quality([small]) == 35
    assert factor_support_count([small]) == 0


def test_small_negative_calibration_can_reduce_but_not_inflate_score() -> None:
    risk = _factor(1, sample_count=5, score=60, stability_score=10, expected_level="风险")
    base_impact = round((risk.score - 50) / 2)

    assert risk.calibration is not None
    assert _factor_score_impact(risk) < base_impact
    assert _factor_calibration_impact(risk.calibration) < 0


def test_real_current_factors_only_aggregate_historically_replayable_definitions() -> None:
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
    calibration_samples = [
        factor.calibration.sample_count
        for factor in factors
        if factor.calibration and factor.calibration.participates_in_historical_aggregate
    ]
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
    assert len(calibration_samples) == 3
    assert 6 <= min(calibration_samples) < 20
    for factor in (factors[2], factors[4], factors[5]):
        assert factor.calibration is not None
        assert factor.calibration.sample_count == 0
        assert factor.calibration.participates_in_historical_aggregate is False
        assert factor.calibration.confidence_level == "当前口径不可回放"
    assert valuation.calibration is not None
    assert valuation.calibration.sample_count == 0
    assert valuation.calibration.confidence_level == "待补数据"
    assert valuation.calibration.participates_in_historical_aggregate is False
    assert metrics.scoring_factor_count == 7
    assert metrics.calibration_factor_count == 3
    assert metrics.uncalibrated_factor_names == ("风险压力", "筹码位置", "龙头强度", "估值锚")
    assert report.calibration_sample_count == min(calibration_samples)
    assert report.evidence_sufficiency == report.calibrated_confidence
    assert report.composite_reliability_level in {"较高", "中等", "较低", "不足"}
    assert all("低置信" not in note for note in report.notes)
    assert any(
        "7 个因子参与评分" in note
        and "3 个参与历史校准" in note
        and "未校准项：风险压力、筹码位置、龙头强度、估值锚" in note
        for note in report.notes
    )


def test_zero_volume_window_is_unavailable_and_has_no_factor_score_weight() -> None:
    analysis, _insights, _feature_snapshot, _chip, _leadership = _fully_calibrated_factor_inputs()
    zero_volume_rows = [row.model_copy(update={"volume": 0}) for row in analysis.klines]
    zero_volume_analysis = build_analysis(
        analysis.quote,
        zero_volume_rows,
        data_quality=analysis.data_quality,
    )
    insights = build_stock_insight_bundle(zero_volume_analysis)
    feature = build_feature_snapshot(zero_volume_analysis, insights)
    chip = build_chip_analysis(zero_volume_analysis, feature)
    leadership = build_leadership_report(zero_volume_analysis, insights, feature)
    factors = build_current_factors(zero_volume_analysis, insights, feature, chip, leadership)
    volume = next(item for item in factors if item.id == "volume_confirmation")

    assert feature.volume_ratio == 0
    assert feature.volume_ratio_available is False
    assert feature.volume_positive_session_count == 0
    assert volume.score == 50
    assert volume.data_nature == "unavailable"
    assert volume.participates_in_current_score is False
    assert volume.calibration is not None
    assert volume.calibration.participates_in_historical_aggregate is False
    assert "完整且为正的20日成交量序列" in volume.missing_data
    assert _weighted_factor_score(factors) == _weighted_factor_score([item for item in factors if item is not volume])


def test_unavailable_order_pressure_text_cannot_change_risk_factor_evidence() -> None:
    analysis, insights, feature, chip, leadership = _fully_calibrated_factor_inputs()
    features = [
        feature.model_copy(
            update={"order_pressure": pressure, "order_pressure_data_nature": "unavailable"}
        )
        for pressure in ("订单压力不可用", "主动卖压", "强买盘")
    ]

    factors = [
        next(
            item
            for item in build_current_factors(analysis, insights, item, chip, leadership)
            if item.id == "risk_pressure"
        )
        for item in features
    ]

    assert factors[0].model_dump() == factors[1].model_dump() == factors[2].model_dump()
    assert "盘口证据不可用" in " ".join(factors[0].evidence)
    assert "主动卖压" not in " ".join(factors[0].evidence)


def test_unavailable_order_pressure_text_cannot_change_leadership_report() -> None:
    analysis, insights, feature, _chip, _leadership = _fully_calibrated_factor_inputs()
    features = [
        feature.model_copy(
            update={"order_pressure": pressure, "order_pressure_data_nature": "unavailable"}
        )
        for pressure in ("订单压力不可用", "主动卖压持续增强", "强买盘占优")
    ]

    reports = [build_leadership_report(analysis, insights, item) for item in features]

    assert reports[0].model_dump() == reports[1].model_dump() == reports[2].model_dump()
    assert "盘口证据不可用" in " ".join(reports[0].evidence)
    assert "主动卖压" not in " ".join(reports[0].evidence)


@pytest.mark.parametrize(
    ("factor_id", "score_field", "availability_updates", "expected_value"),
    [
        (
            "fund_flow_proxy",
            "fund_flow_score",
            {"fund_flow_data_nature": "unavailable"},
            "量价热度证据不可用",
        ),
        (
            "valuation_anchor",
            "valuation_score",
            {"valuation_score_available": False, "valuation_data_nature": "unavailable"},
            "估值证据不可用",
        ),
    ],
)
def test_unavailable_feature_snapshot_scores_cannot_reenter_current_factors(
    factor_id: str,
    score_field: str,
    availability_updates: dict,
    expected_value: str,
) -> None:
    analysis, insights, feature, chip, leadership = _fully_calibrated_factor_inputs()
    features = [
        feature.model_copy(update={score_field: score, **availability_updates})
        for score in (0, 50, 100)
    ]

    factors = [
        next(
            item
            for item in build_current_factors(analysis, insights, snapshot, chip, leadership)
            if item.id == factor_id
        )
        for snapshot in features
    ]

    assert factors[0].model_dump() == factors[1].model_dump() == factors[2].model_dump()
    assert factors[0].score == 50
    assert factors[0].value == expected_value
    assert factors[0].data_nature == "unavailable"
    assert factors[0].participates_in_current_score is False


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
            confidence_level=confidence_level or ("较高" if sample_count >= 20 else "偏低"),
            participates_in_historical_aggregate=(
                participates_in_historical_aggregate and sample_count > 0
            ),
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
    dates = next_trade_dates(date(2025, 12, 31), 100)
    klines = []
    for index in range(100):
        close = 100 + index * 0.5
        row = make_kline(
            date=dates[index].isoformat(),
            close=close,
            high=close + 1,
            low=close - 1,
            volume=1600 + index * 20,
            replay_eligible=True,
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
        pe=24.0,
        pb=4.0,
        market_cap=1_500_000_000_000,
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
