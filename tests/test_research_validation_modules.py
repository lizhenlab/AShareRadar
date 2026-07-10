from __future__ import annotations

from types import SimpleNamespace

from app.models.schemas import FactorCalibration, SignalValidationItem, StandardFactor
from app.services.research_validation import (
    VALIDATION_CONFIDENCE_TIMEFRAME_RULES,
    VALIDATION_OVERALL_RULES,
    VALIDATION_STATUS_RULES,
    _breakout_validation,
    _support_defense_validation,
    _trend_pullback_validation,
    _validation_confidence,
    _validation_notes,
    _validation_overall_status,
    _validation_status,
    _validation_summary,
    _t_range_validation,
)


def test_validation_status_rule_priority_is_explicit() -> None:
    assert [rule.name for rule in VALIDATION_STATUS_RULES] == [
        "environment_suppression",
        "reverse_risk",
        "timeframe_block",
        "weak_factor",
        "mixed_timeframe_confirmed",
        "confirmed",
    ]
    assert _validation_status(True, _regime(1.3), _factor("积极"), _timeframe("高冲突"), reverse=True) == "环境压制"
    assert _validation_status(False, _regime(1.3), _factor("风险"), None, reverse=True) == "环境压制"
    assert _validation_status(False, _regime(1.0), _factor("风险"), None, reverse=True) == "风险触发"
    assert _validation_status(True, _regime(1.0), _factor("积极"), _timeframe("高冲突"), reverse=True) == "周期冲突降级"
    assert _validation_status(True, _regime(1.0), _factor("风险"), _timeframe("多周期顺向"), reverse=True) == "低置信观察"
    assert _validation_status(False, _regime(1.0), None, None, reverse=True) == "风险触发"
    assert _validation_status(True, _regime(1.0), None, _timeframe("中冲突")) == "低置信观察"
    assert _validation_status(True, _regime(1.0), None, None) == "接近确认"
    assert _validation_status(False, _regime(1.0), None, None) == "等待确认"


def test_validation_overall_rule_priority_is_explicit() -> None:
    assert [rule.name for rule in VALIDATION_OVERALL_RULES] == [
        "timeframe_block",
        "risk_priority",
        "mixed_timeframe_second_confirm",
        "multiple_confirmations",
        "single_confirmation",
    ]
    confirmed = [_item("接近确认"), _item("接近确认")]
    assert _validation_overall_status(confirmed, _regime(1.3), _timeframe("高冲突")) == "风险优先"
    assert _validation_overall_status([_item("风险触发"), *confirmed], _regime(1.0), None) == "风险优先"
    assert _validation_overall_status(confirmed, _regime(1.0), _timeframe("中冲突")) == "等待二次确认"
    assert _validation_overall_status(confirmed, _regime(1.08), None) == "条件较好"
    assert _validation_overall_status(confirmed, _regime(1.09), None) == "等待二次确认"
    assert _validation_overall_status([_item("等待确认")], _regime(1.0), None) == "观察为主"


def test_validation_confidence_timeframe_penalties_are_explicit() -> None:
    assert [rule.name for rule in VALIDATION_CONFIDENCE_TIMEFRAME_RULES] == ["blocking_timeframe", "mixed_timeframe"]
    factor = _factor("积极")

    assert _validation_confidence(80, factor, _regime(1.0), _timeframe("高冲突")) == 54
    assert _validation_confidence(80, factor, _regime(1.0), _timeframe("中冲突")) == 60
    assert _validation_confidence(80, factor, _regime(1.0), _timeframe("多周期顺向")) == 66
    assert _validation_confidence(80, None, _regime(1.0), None) == 80


def test_validation_non_finite_numbers_fall_back_to_neutral_defaults() -> None:
    confirmed = [_item("接近确认"), _item("接近确认")]
    non_finite_factor = _raw_factor("积极", score=float("inf"), stability_score=float("nan"))

    assert _validation_status(True, _regime(float("nan")), None, None) == "接近确认"
    assert _validation_status(True, _regime(float("inf")), None, None) == "接近确认"
    assert _validation_overall_status(confirmed, _regime(float("inf")), None) == "条件较好"
    assert _validation_confidence(
        80,
        non_finite_factor,
        _regime(1.0, confidence_adjustment=float("nan")),
        None,
    ) == 50
    assert _validation_summary("观察为主", [], _regime(float("nan")), None) == (
        "观察为主：接近确认的是暂无接近确认的信号；需要防守的是暂无高优先级风险验证项；"
        "环境风险倍率 1.00。"
    )


def test_invalid_ma5_and_resistance_wait_without_promoting_overall_status() -> None:
    for invalid_price in (0, float("nan")):
        trend = _trend_pullback_validation(
            _feature(price=11, ma5=invalid_price, trend_score=80),
            _regime(1.0),
            None,
            None,
        )
        breakout = _breakout_validation(
            _feature(price=11, resistance=invalid_price, volume_ratio=1.5),
            _regime(1.0),
            None,
            None,
        )

        assert trend.status == "等待确认"
        assert breakout.status == "等待确认"
        assert _validation_overall_status([_item("接近确认"), trend, breakout], _regime(1.0), None) == "等待二次确认"


def test_each_validation_waits_when_any_required_price_is_not_positive_and_finite() -> None:
    for invalid_price in (0, float("nan")):
        assert _trend_pullback_validation(
            _feature(price=invalid_price), _regime(1.0), None, None
        ).status == "等待确认"
        assert _breakout_validation(
            _feature(price=invalid_price), _regime(1.0), None, None
        ).status == "等待确认"
        assert _support_defense_validation(
            _feature(price=invalid_price), _regime(1.0), None, None
        ).status == "等待确认"
        assert _support_defense_validation(
            _feature(support=invalid_price), _regime(1.0), None, None
        ).status == "等待确认"
        assert _support_defense_validation(
            _feature(ma20=invalid_price), _regime(1.0), None, None
        ).status == "等待确认"
        assert _t_range_validation(
            _feature(price=invalid_price), _regime(1.0), None, None
        ).status == "等待确认"
        assert _t_range_validation(
            _feature(support=invalid_price), _regime(1.0), None, None
        ).status == "等待确认"
        assert _t_range_validation(
            _feature(resistance=invalid_price), _regime(1.0), None, None
        ).status == "等待确认"


def test_t_range_validation_requires_strict_open_interval_and_precise_text() -> None:
    support_touch = _t_range_validation(_feature(price=10), _regime(1.0), None, None)
    resistance_touch = _t_range_validation(_feature(price=12), _regime(1.0), None, None)
    inside_range = _t_range_validation(_feature(price=11), _regime(1.0), None, None)

    assert support_touch.status == "等待确认"
    assert resistance_touch.status == "等待确认"
    assert inside_range.status == "接近确认"
    assert inside_range.trigger_condition == (
        "价格严格高于支撑 10.00 且低于压力 12.00，不把边界触及当作区间内。"
    )
    assert "不能把做T确认等同于新增仓位" in inside_range.confirmation_condition
    assert "有效突破压力后脱离区间" in inside_range.invalidation_condition


def test_validation_notes_only_warn_for_conservative_timeframes() -> None:
    base_notes = _validation_notes(None)

    assert _validation_notes(_timeframe("多周期顺向")) == base_notes
    assert _validation_notes(_timeframe("中冲突")) == [
        *base_notes,
        "多周期当前为「中冲突」，所有验证状态已按保守口径降级。",
    ]
    assert _validation_notes(_timeframe("多周期偏弱")) == [
        *base_notes,
        "多周期当前为「多周期偏弱」，所有验证状态已按保守口径降级。",
    ]


def test_validation_summary_groups_confirmed_and_defensive_statuses() -> None:
    summary = _validation_summary(
        "等待二次确认",
        [_item("接近确认"), _item("风险触发"), _item("环境压制"), _item("等待确认")],
        _regime(1.234),
        _timeframe("中冲突"),
    )

    assert summary == (
        "等待二次确认：接近确认的是接近确认 项；需要防守的是风险触发 项、环境压制 项；"
        "环境风险倍率 1.23；多周期为「中冲突」。"
    )


def _regime(risk_multiplier: float, confidence_adjustment: object = 0):
    return SimpleNamespace(risk_multiplier=risk_multiplier, confidence_adjustment=confidence_adjustment)


def _timeframe(conflict_level: str):
    return SimpleNamespace(conflict_level=conflict_level)


def _factor(expected_level: str) -> StandardFactor:
    return StandardFactor(
        id="test",
        name="测试因子",
        category="测试",
        value="测试",
        score=60,
        level="观察",
        direction="中性",
        weight=1,
        calibration=FactorCalibration(
            sample_count=10,
            win_rate=50,
            avg_forward_5d_return=0,
            avg_forward_10d_return=0,
            max_adverse_return=0,
            stability_score=50,
            expected_level=expected_level,
            confidence_level="中",
            note="测试校准",
        ),
    )


def _raw_factor(expected_level: str, *, score: object, stability_score: object):
    return SimpleNamespace(
        score=score,
        calibration=SimpleNamespace(expected_level=expected_level, stability_score=stability_score),
    )


def _feature(**updates: float):
    values = {
        "price": 11,
        "support": 10,
        "resistance": 12,
        "ma5": 10.5,
        "ma20": 10,
        "trend_score": 70,
        "volume_ratio": 1.2,
        "signal_confidence": 70,
        "data_quality_score": 80,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _item(status: str) -> SignalValidationItem:
    return SignalValidationItem(
        name=f"{status} 项",
        category="测试",
        status=status,
        confidence=50,
        trigger_condition="触发",
        confirmation_condition="确认",
        invalidation_condition="失效",
        historical_reference="历史参考",
        action_hint="动作提示",
    )
