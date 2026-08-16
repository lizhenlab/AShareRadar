from __future__ import annotations

import math
from types import SimpleNamespace

from app.services.research_t_strategy import (
    T_STRATEGY_STYLE_RULES,
    T_STRATEGY_SUITABILITY_RULES,
    _t_strategy_style,
    build_t_strategy_assistant_report,
)


def test_t_strategy_rule_priority_is_explicit() -> None:
    assert [rule.name for rule in T_STRATEGY_STYLE_RULES] == ["risk_defensive", "narrow_waiting", "trend_rolling"]
    assert [rule.name for rule in T_STRATEGY_SUITABILITY_RULES] == ["active_t_blocked", "tradable_range"]


def test_t_strategy_blocks_active_t_when_data_or_market_risk_is_high() -> None:
    weak_quality = build_t_strategy_assistant_report(_analysis(65), _feature(), _regime(1.0), _validation("条件较好"))
    risky_market = build_t_strategy_assistant_report(_analysis(90), _feature(), _regime(1.28), _validation("条件较好"))

    assert weak_quality.suitability == "不适合主动做T"
    assert risky_market.suitability == "不适合主动做T"


def test_t_strategy_requires_tradable_range_and_non_risk_validation() -> None:
    tradable = build_t_strategy_assistant_report(_analysis(90), _feature(), _regime(1.0), _validation("条件较好"))
    risk_priority = build_t_strategy_assistant_report(_analysis(90), _feature(), _regime(1.0), _validation("风险优先"))
    narrow = build_t_strategy_assistant_report(_analysis(90), _feature(support=99.4, resistance=100.4), _regime(1.0), _validation("条件较好"))

    assert tradable.suitability == "仅底仓可做T"
    assert tradable.low_zone == "98.00 附近"
    assert tradable.high_zone == "102.00 附近"
    assert risk_priority.suitability == "等待更大区间"
    assert narrow.suitability == "等待更大区间"


def test_t_strategy_style_keeps_risk_and_width_priority() -> None:
    assert _t_strategy_style(_feature(), _regime(1.25)) == "风险防守型"
    assert _t_strategy_style(_feature(support=99.4, resistance=100.4, trend_score=80), _regime(1.0)) == "窄幅等待型"
    assert _t_strategy_style(_feature(trend_score=70, ma5=99), _regime(1.0)) == "趋势滚动型"
    assert _t_strategy_style(_feature(trend_score=55, ma5=101), _regime(1.0)) == "区间震荡型"


def test_t_strategy_sanitizes_non_finite_feature_values() -> None:
    report = build_t_strategy_assistant_report(
        _analysis(90),
        _feature(price=math.nan, support=math.inf, resistance=math.inf, atr14=math.inf, atr_pct=math.nan, ma5=math.inf),
        _regime(math.inf),
        _validation("条件较好"),
    )

    assert report.style == "窄幅等待型"
    assert report.suitability == "等待更大区间"
    assert report.low_zone == "待确认"
    assert report.high_zone == "待确认"
    assert report.stop_conditions[0] == "支撑位待确认，若放量下跌或区间边界失效则停止做T。"


def test_t_strategy_uses_price_buffer_when_resistance_is_missing() -> None:
    report = build_t_strategy_assistant_report(
        _analysis(90),
        _feature(price=100, support=95, resistance=math.nan, atr14=2, atr_pct=2),
        _regime(1.0),
        _validation("条件较好"),
    )

    assert report.low_zone == "98.00 附近"
    assert report.high_zone == "102.00 附近"


def test_unavailable_t_strategy_inputs_never_generate_actions_or_price_zones() -> None:
    first = build_t_strategy_assistant_report(
        _analysis(90),
        _feature(
            support=95,
            resistance=107,
            atr14=2,
            atr_pct=2,
            support_available=False,
            resistance_available=False,
            atr14_available=False,
        ),
        _regime(1.0),
        _validation("条件较好"),
    )
    perturbed = build_t_strategy_assistant_report(
        _analysis(90),
        _feature(
            support=1,
            resistance=999,
            atr14=88,
            atr_pct=88,
            support_available=False,
            resistance_available=False,
            atr14_available=False,
        ),
        _regime(1.0),
        _validation("条件较好"),
    )

    assert first == perturbed
    assert first.suitability == "等待更大区间"
    assert first.low_zone == first.high_zone == "待确认"
    assert all("低吸只看" not in item and "高抛只看" not in item for item in first.execution_steps)


def _analysis(data_quality_score: int):
    return SimpleNamespace(
        quote=SimpleNamespace(code="600519", market="SH", timestamp="2026-05-13 10:00:00"),
        data_quality=SimpleNamespace(score=data_quality_score),
    )


def _feature(
    *,
    price: float = 100,
    support: float = 95,
    resistance: float = 107,
    atr14: float = 2,
    atr_pct: float = 2,
    trend_score: int = 70,
    ma5: float = 99,
    support_available: bool = True,
    resistance_available: bool = True,
    atr14_available: bool = True,
):
    return SimpleNamespace(
        price=price,
        support=support,
        resistance=resistance,
        atr14=atr14,
        atr_pct=atr_pct,
        trend_score=trend_score,
        ma5=ma5,
        support_available=support_available,
        resistance_available=resistance_available,
        atr14_available=atr14_available,
    )


def _regime(risk_multiplier: float):
    return SimpleNamespace(risk_multiplier=risk_multiplier)


def _validation(overall_status: str):
    return SimpleNamespace(overall_status=overall_status)
