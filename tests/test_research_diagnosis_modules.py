from __future__ import annotations

from datetime import date, datetime, timedelta

from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research import (
    build_alpha_evidence_report,
    build_chip_analysis,
    build_factor_lab_report,
    build_feature_snapshot,
    build_leadership_report,
    build_market_regime_report,
    build_risk_reward_report,
    build_signal_validation_report,
    build_stock_diagnosis,
    build_timeframe_alignment_report,
)
from app.services.stock_insights import build_stock_insight_bundle
from app.services.research_diagnosis_decisions import final_diagnosis_action, diagnosis_headline
from app.services.research_diagnosis_sections import build_confirmation_signals, build_hard_risks, build_watch_focus, main_conflict_sentence
from tests.factories import make_kline, make_quote


def test_data_quality_blocks_active_diagnosis_action() -> None:
    analysis, _bundle, feature, alpha, factor_lab, regime, validation, risk_reward, timeframe = _diagnosis_inputs()
    low_quality_analysis = analysis.model_copy(update={"data_quality": analysis.data_quality.model_copy(update={"score": 42})})
    low_quality_feature = feature.model_copy(update={"data_quality_score": 42})

    assert final_diagnosis_action(low_quality_analysis, alpha, validation, risk_reward, timeframe, regime) == "控制风险"
    assert diagnosis_headline(low_quality_analysis, low_quality_feature, alpha, factor_lab, regime, validation, risk_reward, timeframe) == "数据质量不足，先暂停主动买卖判断"


def test_weak_timeframe_can_wait_when_other_evidence_is_not_defensive() -> None:
    analysis, _bundle, _feature, alpha, _factor_lab, regime, validation, risk_reward, timeframe = _diagnosis_inputs()
    weak_timeframe = timeframe.model_copy(update={"conflict_level": "多周期偏弱"})
    watch_validation = validation.model_copy(update={"overall_status": "观察为主"})
    good_risk_reward = risk_reward.model_copy(update={"rating": "性价比较好", "reward_risk_ratio": 1.55})
    positive_alpha = alpha.model_copy(update={"verdict": "积极证据占优"})

    assert final_diagnosis_action(analysis, positive_alpha, watch_validation, good_risk_reward, weak_timeframe, regime) == "等待确认"


def test_high_timeframe_conflict_is_visible_in_final_diagnosis() -> None:
    analysis, bundle, feature, alpha, factor_lab, regime, validation, risk_reward, timeframe = _diagnosis_inputs()
    high_conflict = timeframe.model_copy(update={"conflict_level": "高冲突", "alignment_label": "短强长弱"})
    neutral_validation = validation.model_copy(update={"overall_status": "观察为主"})
    neutral_risk_reward = risk_reward.model_copy(update={"rating": "性价比较好", "reward_risk_ratio": 1.60})

    diagnosis = build_stock_diagnosis(analysis, bundle, feature, alpha, factor_lab, regime, neutral_validation, neutral_risk_reward, high_conflict)

    assert diagnosis.action == "控制风险"
    assert diagnosis.headline == "多周期冲突明显，先收缩判断"
    assert any("高冲突" in item for item in diagnosis.hard_risks)


def test_diagnosis_headline_rules_keep_defensive_priority() -> None:
    analysis, _bundle, feature, alpha, factor_lab, regime, validation, risk_reward, timeframe = _diagnosis_inputs()
    low_quality_feature = feature.model_copy(update={"data_quality_score": 42})
    high_conflict = timeframe.model_copy(update={"conflict_level": "高冲突"})
    positive_alpha = alpha.model_copy(update={"verdict": "积极证据占优"})
    strong_factor_lab = factor_lab.model_copy(update={"total_score": 80, "calibrated_confidence": 75})

    headline = diagnosis_headline(analysis, low_quality_feature, positive_alpha, strong_factor_lab, regime, validation, risk_reward, high_conflict)

    assert headline == "数据质量不足，先暂停主动买卖判断"


def test_positive_factor_headline_loses_to_environment_risk() -> None:
    analysis, _bundle, feature, alpha, factor_lab, regime, validation, risk_reward, timeframe = _diagnosis_inputs()
    strong_factor_lab = factor_lab.model_copy(update={"total_score": 80, "calibrated_confidence": 75})
    high_risk_regime = regime.model_copy(update={"risk_multiplier": 1.35})
    neutral_validation = validation.model_copy(update={"overall_status": "观察为主"})
    good_risk_reward = risk_reward.model_copy(update={"rating": "性价比较好", "reward_risk_ratio": 1.60})
    aligned_timeframe = timeframe.model_copy(update={"conflict_level": "多周期顺向"})

    headline = diagnosis_headline(analysis, feature, alpha, strong_factor_lab, high_risk_regime, neutral_validation, good_risk_reward, aligned_timeframe)

    assert headline == "环境风险偏高，先缩小判断半径"


def test_final_action_rules_keep_risk_and_wait_priority_before_positive_alpha() -> None:
    analysis, _bundle, _feature, alpha, _factor_lab, regime, validation, risk_reward, timeframe = _diagnosis_inputs()
    hold_analysis = analysis.model_copy(update={"action_advice": analysis.action_advice.model_copy(update={"action": "持有观察"})})
    positive_alpha = alpha.model_copy(update={"verdict": "积极证据占优"})
    neutral_validation = validation.model_copy(update={"overall_status": "观察为主"})
    aligned_timeframe = timeframe.model_copy(update={"conflict_level": "多周期顺向"})
    normal_regime = regime.model_copy(update={"risk_multiplier": 1.0})

    control_risk_reward = risk_reward.model_copy(update={"rating": "风险优先"})
    wait_risk_reward = risk_reward.model_copy(update={"rating": "等待确认"})
    good_risk_reward = risk_reward.model_copy(update={"rating": "性价比较好", "reward_risk_ratio": 1.60})

    assert final_diagnosis_action(hold_analysis, positive_alpha, neutral_validation, control_risk_reward, aligned_timeframe, normal_regime) == "控制风险"
    assert final_diagnosis_action(hold_analysis, positive_alpha, neutral_validation, wait_risk_reward, aligned_timeframe, normal_regime) == "等待确认"
    assert final_diagnosis_action(hold_analysis, positive_alpha, neutral_validation, good_risk_reward, aligned_timeframe, normal_regime) == "积极关注"


def test_diagnosis_sections_add_contextual_confirmations_and_risks() -> None:
    _analysis, bundle, feature, _alpha, factor_lab, regime, _validation, risk_reward, timeframe = _diagnosis_inputs()
    weak_feature = feature.model_copy(update={"trend_score": 45})
    positive_factor_lab = factor_lab.model_copy(update={"top_positive": ["趋势强度"]})
    weak_timeframe = timeframe.model_copy(update={"conflict_level": "中冲突"})
    weak_risk_reward = risk_reward.model_copy(update={"rating": "性价比不足"})

    confirmations = build_confirmation_signals(weak_feature, positive_factor_lab, timeframe)
    risks = build_hard_risks(feature, bundle, factor_lab, regime, weak_risk_reward, weak_timeframe)

    assert confirmations[0].startswith("趋势评分重新回到 55 分以上")
    assert any("因子" in item for item in confirmations)
    assert any("性价比不足" in item for item in risks)
    assert any("中冲突" in item for item in risks)


def test_diagnosis_sections_skip_missing_key_price_text() -> None:
    _analysis, bundle, feature, _alpha, factor_lab, _regime, _validation, _risk_reward, timeframe = _diagnosis_inputs()
    missing_levels = feature.model_copy(update={"ma5": 0, "ma20": 0, "support": 0, "resistance": 0})

    confirmations = build_confirmation_signals(missing_levels, factor_lab, timeframe)
    risks = build_hard_risks(missing_levels, bundle)

    assert all("0.00" not in item for item in confirmations)
    assert all("0.00" not in item for item in risks)
    assert "数据质量降到" in risks[-1]


def test_watch_focus_deduplicates_contextual_suggestions() -> None:
    _analysis, _bundle, feature, _alpha, factor_lab, regime, validation, _risk_reward, timeframe = _diagnosis_inputs()
    duplicate = "先看关键价位，再看量能和资金是否确认。"
    regime = regime.model_copy(update={"suggestions": [duplicate, "行业环境若转弱，降低信号权重。"]})
    timeframe = timeframe.model_copy(update={"suggestions": ["行业环境若转弱，降低信号权重。"]})

    focus = build_watch_focus(feature, factor_lab, regime, validation, timeframe)

    assert focus.count(duplicate) == 1
    assert focus.count("行业环境若转弱，降低信号权重。") == 1


def test_main_conflict_sentence_normalizes_period() -> None:
    assert main_conflict_sentence("趋势确认和风险控制") == "当前主要矛盾是：趋势确认和风险控制。"
    assert main_conflict_sentence("当前主要矛盾是趋势确认") == "当前主要矛盾是趋势确认。"


def _diagnosis_inputs():
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
    regime = build_market_regime_report(analysis, bundle, feature, factor_lab)
    timeframe = build_timeframe_alignment_report(analysis, feature, factor_lab)
    validation = build_signal_validation_report(analysis, feature, factor_lab, regime, timeframe)
    risk_reward = build_risk_reward_report(analysis, feature, factor_lab, regime, validation, timeframe)
    alpha = build_alpha_evidence_report(analysis, bundle, feature, factor_lab, regime, timeframe, risk_reward)
    return analysis, bundle, feature, alpha, factor_lab, regime, validation, risk_reward, timeframe
