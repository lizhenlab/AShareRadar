from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
import math

from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_breadth import build_market_breadth_snapshot
from app.services.research_chip import build_chip_analysis
from app.services.research_features import build_feature_snapshot, build_leadership_report
from app.services.research_factors import build_factor_lab_report
from app.services.research_regime import (
    MIN_FACTOR_RISK_REDUCTION_SAMPLES,
    _build_regime_context,
    _factor_lab_risk_adjustment,
    _market_regime_label,
    _regime_risk_adjustments,
    build_market_regime_report,
)
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote


def test_missing_price_levels_do_not_fake_pressure_state() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()
    safe_analysis, safe_bundle = _safe_inputs(analysis, bundle)
    neutral_feature = feature.model_copy(
        update={
            "support": 0,
            "resistance": 0,
            "trend_score": 52,
            "fund_flow_score": 50,
            "data_quality_score": 90,
        }
    )
    neutral_factor_lab = factor_lab.model_copy(
        update={
            "total_score": 52,
            "positive_factor_count": 1,
            "negative_factor_count": 1,
            "top_positive": [],
            "top_negative": [],
        }
    )

    regime = build_market_regime_report(safe_analysis, safe_bundle, neutral_feature, neutral_factor_lab)

    assert regime.stock_state == "震荡等待"
    assert all("压力位 0.00" not in item for item in regime.suggestions)


def test_low_quality_has_priority_over_trade_location() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()
    low_quality_feature = feature.model_copy(
        update={
            "data_quality_score": 45,
            "support": feature.price,
            "resistance": feature.price,
        }
    )

    regime = build_market_regime_report(analysis, bundle, low_quality_feature, factor_lab)

    assert regime.stock_state == "数据不足"
    assert regime.market_label == "低置信环境"
    assert regime.risk_multiplier >= 1.2
    assert regime.suggestions[0].startswith("先恢复数据质量")


def test_factor_edges_adjust_risk_multiplier_and_suggestions() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()
    positive_factor_lab = factor_lab.model_copy(
        update={
            "total_score": 70,
            "calibrated_confidence": 70,
            "calibration_sample_count": 30,
            "positive_factor_count": 5,
            "negative_factor_count": 1,
            "top_positive": ["趋势强度"],
            "top_negative": [],
        }
    )
    negative_factor_lab = factor_lab.model_copy(
        update={
            "total_score": 42,
            "calibrated_confidence": 45,
            "calibration_sample_count": 30,
            "positive_factor_count": 1,
            "negative_factor_count": 4,
            "top_negative": ["量能背离"],
        }
    )

    positive_regime = build_market_regime_report(analysis, bundle, feature, positive_factor_lab)
    negative_regime = build_market_regime_report(analysis, bundle, feature, negative_factor_lab)

    assert positive_regime.risk_multiplier < negative_regime.risk_multiplier
    assert any("正向因子" in item for item in positive_regime.suggestions)
    assert any("量能背离" in item for item in negative_regime.suggestions)


def test_market_breadth_controls_environment_before_stock_tailwind() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()
    strong_feature = feature.model_copy(update={"trend_score": 76, "fund_flow_score": 70})
    strong_factor_lab = factor_lab.model_copy(update={"total_score": 72, "calibrated_confidence": 68})
    cold_breadth = build_market_breadth_snapshot(
        [make_quote(price=10, prev_close=10.5, high=10.7, low=9.8, change_pct=-4.8) for _ in range(10)]
    )
    warm_breadth = build_market_breadth_snapshot(
        [make_quote(price=10.7, prev_close=10, high=10.9, low=9.9, change_pct=7.0) for _ in range(10)]
    )

    cold_regime = build_market_regime_report(analysis, bundle, strong_feature, strong_factor_lab, cold_breadth)
    warm_regime = build_market_regime_report(analysis, bundle, strong_feature, strong_factor_lab, warm_breadth)

    assert cold_regime.market_label == "市场偏冷环境"
    assert cold_regime.risk_multiplier > warm_regime.risk_multiplier
    assert any("市场宽度偏冷" in item for item in cold_regime.suggestions)


def test_market_regime_surfaces_breadth_source_degradation_conservatively() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()
    warning = "市场宽度数据源请求失败，环境判断已降级。"
    breadth = build_market_breadth_snapshot([], warnings=[warning])

    regime = build_market_regime_report(analysis, bundle, feature, factor_lab, breadth)

    assert regime.breadth_label == "市场宽度数据降级"
    assert regime.breadth_score == 45
    assert any(warning in item for item in regime.evidence)
    assert any("不据此上调环境评级" in item for item in regime.suggestions)


def test_market_regime_evidence_uses_source_level_non_statistical_semantics() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()

    regime = build_market_regime_report(analysis, bundle, feature, factor_lab)
    evidence = " ".join(regime.evidence)

    assert "证据充分度" in evidence
    assert "/100" in evidence
    assert "量价热度（衍生）" in evidence
    assert "校准置信度" not in evidence
    assert "资金评分" not in evidence
    assert regime.confidence_adjustment_semantics == "non_statistical_evidence_sufficiency_adjustment"


def test_market_regime_label_rules_keep_priority_explicit() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()
    safe_analysis, safe_bundle = _safe_inputs(analysis, bundle)
    neutral_feature = feature.model_copy(
        update={
            "data_quality_score": 90,
            "trend_score": 52,
            "fund_flow_score": 50,
            "industry_name": "测试行业",
            "industry_change_pct": 0,
        }
    )
    neutral_factor_lab = factor_lab.model_copy(update={"total_score": 52})
    cold_breadth = build_market_breadth_snapshot(
        [make_quote(price=10, prev_close=10.5, high=10.7, low=9.8, change_pct=-4.8) for _ in range(10)]
    )
    warm_breadth = build_market_breadth_snapshot(
        [make_quote(price=10.7, prev_close=10, high=10.9, low=9.9, change_pct=7.0) for _ in range(10)]
    )

    def market_label(feature_updates=None, factor_updates=None, breadth=None, stock_state="震荡等待") -> str:
        context = _build_regime_context(
            safe_analysis,
            safe_bundle,
            neutral_feature.model_copy(update=feature_updates or {}),
            neutral_factor_lab.model_copy(update=factor_updates or {}),
            breadth,
        )
        return _market_regime_label(context, stock_state)

    assert market_label({"data_quality_score": 45}, {"total_score": 72}, cold_breadth) == "低置信环境"
    assert market_label(stock_state="风险优先") == "风险环境"
    assert market_label({"industry_change_pct": 1.5}, {"total_score": 72}, cold_breadth) == "市场偏冷环境"
    assert market_label({"industry_change_pct": 0.5}, {"total_score": 72}) == "个股顺风环境"
    assert market_label({"industry_change_pct": -2.0}, {"total_score": 52}, warm_breadth) == "市场偏暖环境"
    assert market_label({"industry_change_pct": -2.0}, {"total_score": 52}) == "行业逆风环境"
    assert market_label() == "中性观察环境"


def test_factor_lab_risk_adjustment_rules_scale_with_data_quality() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()
    positive_factor_lab = factor_lab.model_copy(
        update={
            "total_score": 70,
            "calibrated_confidence": 70,
            "calibration_sample_count": 30,
            "positive_factor_count": 5,
            "negative_factor_count": 1,
        }
    )
    negative_factor_lab = factor_lab.model_copy(
        update={
            "total_score": 42,
            "positive_factor_count": 1,
            "negative_factor_count": 4,
        }
    )

    def adjustment(data_quality_score: int, lab=positive_factor_lab) -> float:
        context = _build_regime_context(
            analysis,
            bundle,
            feature.model_copy(update={"data_quality_score": data_quality_score}),
            lab,
            None,
        )
        return _factor_lab_risk_adjustment(context)

    assert round(adjustment(90), 2) == -0.14
    assert round(adjustment(60), 2) == -0.07
    assert adjustment(45) == 0
    assert round(adjustment(90, negative_factor_lab), 2) == 0.17


def test_factor_lab_does_not_reduce_risk_for_six_thin_overlapping_factor_samples() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()
    thin_factor_lab = factor_lab.model_copy(
        update={
            "total_score": 70,
            "calibrated_confidence": 70,
            "calibration_sample_count": 4,
            "positive_factor_count": 5,
            "negative_factor_count": 1,
        }
    )
    context = _build_regime_context(analysis, bundle, feature, thin_factor_lab, None)

    assert _factor_lab_risk_adjustment(context) == 0


def test_real_six_factor_sample_floor_reaches_factor_risk_reduction_rules() -> None:
    analysis, bundle, feature, factor_lab = _fully_calibrated_regime_inputs()
    context = _build_regime_context(analysis, bundle, feature, factor_lab, None)

    assert len(factor_lab.factors) == 7
    assert factor_lab.calibration_sample_count >= MIN_FACTOR_RISK_REDUCTION_SAMPLES
    assert factor_lab.factors[-1].id == "valuation_anchor"
    assert factor_lab.factors[-1].calibration is not None
    assert factor_lab.factors[-1].calibration.sample_count == 0
    assert round(_factor_lab_risk_adjustment(context), 2) == -0.14


def test_regime_risk_adjustments_are_named_and_ignore_non_finite_values() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()
    neutral_breadth = build_market_breadth_snapshot([])
    bad_breadth = replace(neutral_breadth, risk_adjustment=math.inf)
    context = _build_regime_context(analysis, bundle, feature, factor_lab, neutral_breadth)

    assert [item.name for item in _regime_risk_adjustments(context)] == [
        "data_quality",
        "analysis_risk",
        "abnormal_event",
        "industry",
        "factor_lab",
        "market_breadth",
    ]
    bad_regime = build_market_regime_report(analysis, bundle, feature, factor_lab, bad_breadth)
    neutral_regime = build_market_regime_report(analysis, bundle, feature, factor_lab, neutral_breadth)

    assert bad_regime.risk_multiplier == neutral_regime.risk_multiplier


def test_non_finite_metrics_do_not_leak_into_report_payload() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()
    safe_analysis, safe_bundle = _safe_inputs(analysis, bundle)
    noisy_feature = feature.model_copy(
        update={
            "data_quality_score": math.nan,
            "trend_score": math.inf,
            "fund_flow_score": math.nan,
            "price": math.inf,
            "support": math.inf,
            "resistance": math.nan,
            "ma5": math.inf,
            "industry_name": "测试行业",
            "industry_change_pct": math.nan,
        }
    )
    noisy_factor_lab = factor_lab.model_copy(
        update={
            "total_score": math.nan,
            "calibrated_confidence": math.inf,
            "calibration_sample_count": math.inf,
            "positive_factor_count": math.inf,
            "negative_factor_count": 0,
            "top_positive": [],
            "top_negative": [],
        }
    )
    noisy_breadth = replace(build_market_breadth_snapshot([]), score=math.nan, risk_adjustment=math.inf)

    regime = build_market_regime_report(safe_analysis, safe_bundle, noisy_feature, noisy_factor_lab, noisy_breadth)
    rendered = " ".join([*regime.evidence, *regime.suggestions]).lower()

    assert regime.stock_state == "数据不足"
    assert regime.market_label == "低置信环境"
    assert regime.industry_label == "行业待确认"
    assert regime.breadth_score == 50
    assert 0.72 <= regime.risk_multiplier <= 1.48
    assert math.isfinite(regime.risk_multiplier)
    assert isinstance(regime.confidence_adjustment, int)
    assert "nan" not in rendered
    assert "inf" not in rendered


def test_right_side_suggestion_uses_clean_price_text_for_invalid_levels() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()
    safe_analysis, safe_bundle = _safe_inputs(analysis, bundle)
    strong_feature = feature.model_copy(
        update={
            "data_quality_score": 90,
            "trend_score": 76,
            "fund_flow_score": 70,
            "ma5": math.inf,
            "resistance": math.nan,
        }
    )
    strong_factor_lab = factor_lab.model_copy(update={"total_score": 72, "top_positive": [], "top_negative": []})

    regime = build_market_regime_report(safe_analysis, safe_bundle, strong_feature, strong_factor_lab)
    rendered = " ".join(regime.suggestions).lower()

    assert regime.stock_state == "右侧偏强"
    assert any("压力位 待确认" in item for item in regime.suggestions)
    assert "nan" not in rendered
    assert "inf" not in rendered


def test_blank_optional_text_fields_do_not_leak_into_regime_output() -> None:
    analysis, bundle, feature, factor_lab = _regime_inputs()
    safe_analysis, safe_bundle = _safe_inputs(analysis, bundle)
    feature = feature.model_copy(
        update={
            "data_quality_score": 90,
            "trend_score": 60,
            "fund_flow_score": 55,
            "industry_name": "  ",
            "industry_change_pct": 2.5,
        }
    )
    factor_lab = factor_lab.model_copy(
        update={
            "total_score": 52,
            "top_positive": [],
            "top_negative": ["", "  ", "量能背离"],
        }
    )
    breadth = replace(build_market_breadth_snapshot([]), summary=" ")

    regime = build_market_regime_report(safe_analysis, safe_bundle, feature, factor_lab, breadth)
    rendered = " ".join([*regime.evidence, *regime.suggestions])

    assert regime.industry_label == "行业待确认"
    assert "行业   涨跌幅" not in rendered
    assert "拖累因子「」" not in rendered
    assert "拖累因子「量能背离」" in rendered
    assert "市场宽度样本不足，环境参考待确认。" in regime.evidence


def _safe_inputs(analysis, bundle):
    return (
        analysis.model_copy(update={"risk_level": "低风险"}),
        bundle.model_copy(
            update={
                "abnormal_events": bundle.abnormal_events.model_copy(update={"level": "观察"}),
            }
        ),
    )


def _regime_inputs():
    start = date(2026, 3, 27)
    klines = [
        make_kline(
            date=(start + timedelta(days=index)).isoformat(),
            close=100 + index * 0.85,
            high=102 + index * 0.85,
            low=98 + index * 0.85,
            volume=1600 + index * 60,
        )
        for index in range(48)
    ]
    quote = make_quote(price=142.0, prev_close=139.0, high=144.0, low=138.5, change_pct=2.16, turnover_rate=4.2)
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
    analysis = build_analysis(quote, klines, data_quality=quality)
    bundle = build_stock_insight_bundle(analysis)
    feature = build_feature_snapshot(analysis, bundle)
    chip = build_chip_analysis(analysis, feature)
    leadership = build_leadership_report(analysis, bundle, feature)
    factor_lab = build_factor_lab_report(analysis, bundle, feature, chip, leadership)
    return analysis, bundle, feature, factor_lab


def _fully_calibrated_regime_inputs():
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
    bundle = build_stock_insight_bundle(analysis)
    feature = build_feature_snapshot(analysis, bundle)
    chip = build_chip_analysis(analysis, feature)
    leadership = build_leadership_report(analysis, bundle, feature)
    factor_lab = build_factor_lab_report(analysis, bundle, feature, chip, leadership)
    return analysis, bundle, feature, factor_lab
