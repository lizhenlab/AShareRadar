from __future__ import annotations

from types import SimpleNamespace

from app.services.research_factor_scoring import (
    CHIP_DISTANCE_RULES,
    CHIP_FALLBACK_RULES,
    RISK_PRESSURE_RULES,
    VOLUME_CONFIRMATION_RULES,
    _chip_concentration_adjustment,
    _chip_distance_adjustment,
    _chip_position_score_current,
    _risk_pressure_score,
    _volume_confirmation_score,
)


def test_volume_confirmation_rules_keep_priority_and_boundaries() -> None:
    assert [rule.name for rule in VOLUME_CONFIRMATION_RULES] == [
        "positive_volume_expansion",
        "negative_volume_expansion",
        "low_volume_large_move",
        "normal_volume",
    ]
    assert _volume_confirmation_score(_analysis(change_pct=2), _feature(volume_ratio=1.2)) == 70
    assert _volume_confirmation_score(_analysis(change_pct=-2), _feature(volume_ratio=1.2)) == 34
    assert _volume_confirmation_score(_analysis(change_pct=2), _feature(volume_ratio=0.69)) == 44
    assert _volume_confirmation_score(_analysis(change_pct=1.9), _feature(volume_ratio=0.69)) == 52
    assert _volume_confirmation_score(_analysis(change_pct=0), _feature(volume_ratio=0.85)) == 56
    assert _volume_confirmation_score(_analysis(change_pct=0), _feature(volume_ratio=1.25)) == 56


def test_volume_confirmation_expansion_bonus_is_capped() -> None:
    assert _volume_confirmation_score(_analysis(change_pct=5), _feature(volume_ratio=2.5)) == 80
    assert _volume_confirmation_score(_analysis(change_pct=-5), _feature(volume_ratio=2.5)) == 24


def test_risk_pressure_rules_keep_priority_and_combined_adjustments() -> None:
    assert [rule.name for rule in RISK_PRESSURE_RULES] == ["risk_level", "abnormal_risk", "sell_pressure", "below_ma20"]
    score = _risk_pressure_score(
        SimpleNamespace(risk_level="高风险"),
        SimpleNamespace(abnormal_events=SimpleNamespace(level="风险")),
        _feature(data_quality_score=60, order_pressure="卖压偏强", price=90, ma20=100),
    )

    assert score == 6
    assert _risk_pressure_score(
        SimpleNamespace(risk_level="低风险"),
        SimpleNamespace(abnormal_events=SimpleNamespace(level="平稳")),
        _feature(data_quality_score=90, order_pressure="均衡", price=105, ma20=100),
    ) == 80


def test_chip_fallback_score_uses_explicit_price_location_priority() -> None:
    assert [rule.name for rule in CHIP_FALLBACK_RULES] == ["near_resistance", "near_support"]
    assert _chip_position_score_current(_feature(price=99, support=80, resistance=100), None) == 54
    assert _chip_position_score_current(_feature(price=82, support=80, resistance=100), None) == 48
    assert _chip_position_score_current(_feature(price=90, support=80, resistance=100), None) == 52
    assert _chip_position_score_current(_feature(price=99, support=99, resistance=100), None) == 54


def test_chip_distance_rules_keep_boundaries_stable() -> None:
    assert [rule.name for rule in CHIP_DISTANCE_RULES] == [
        "near_cost_center",
        "moderately_above_center",
        "overheated_above_center",
        "deep_below_center",
    ]
    assert _chip_distance_adjustment(-3) == 16
    assert _chip_distance_adjustment(8) == 16
    assert _chip_distance_adjustment(8.1) == 4
    assert _chip_distance_adjustment(16) == 4
    assert _chip_distance_adjustment(16.1) == -14
    assert _chip_distance_adjustment(-8) == 0
    assert _chip_distance_adjustment(-8.1) == -12


def test_chip_position_score_combines_base_distance_and_concentration() -> None:
    assert _chip_position_score_current(_feature(price=105), _chip(center_price=100, concentration=60)) == 76
    assert _chip_position_score_current(_feature(price=112), _chip(center_price=100, concentration=50)) == 62
    assert _chip_position_score_current(_feature(price=120), _chip(center_price=100, concentration=50)) == 44
    assert _chip_position_score_current(_feature(price=89), _chip(center_price=100, concentration=50)) == 46
    assert _chip_position_score_current(_feature(price=95), _chip(center_price=100, concentration=50)) == 58
    assert _chip_concentration_adjustment(60) == 2


def _analysis(*, change_pct: float):
    return SimpleNamespace(quote=SimpleNamespace(change_pct=change_pct))


def _feature(
    *,
    price: float = 90,
    support: float = 80,
    resistance: float = 120,
    volume_ratio: float = 1.0,
    data_quality_score: int = 80,
    order_pressure: str = "均衡",
    ma20: float = 100,
):
    return SimpleNamespace(
        price=price,
        support=support,
        resistance=resistance,
        volume_ratio=volume_ratio,
        data_quality_score=data_quality_score,
        order_pressure=order_pressure,
        ma20=ma20,
    )


def _chip(*, center_price: float, concentration: int):
    return SimpleNamespace(center_price=center_price, concentration=concentration)
