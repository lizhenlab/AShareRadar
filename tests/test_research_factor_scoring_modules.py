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


def test_unavailable_volume_ratio_is_neutral_instead_of_normal_volume_bonus() -> None:
    assert (
        _volume_confirmation_score(
            _analysis(change_pct=3),
            _feature(volume_ratio=1.0, volume_ratio_available=False),
        )
        == 50
    )


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


def test_unavailable_ma20_and_order_pressure_values_do_not_change_risk_score() -> None:
    analysis = SimpleNamespace(risk_level="低风险")
    insights = SimpleNamespace(abnormal_events=SimpleNamespace(level="平稳"))
    first = _feature(
        price=90,
        ma20=100,
        ma20_available=False,
        order_pressure="卖压偏强",
        order_pressure_data_nature="unavailable",
    )
    perturbed = _feature(
        price=90,
        ma20=1,
        ma20_available=False,
        order_pressure="买压极强",
        order_pressure_data_nature="unavailable",
    )

    assert _risk_pressure_score(analysis, insights, first) == _risk_pressure_score(analysis, insights, perturbed)


def test_chip_fallback_score_uses_explicit_price_location_priority() -> None:
    assert [rule.name for rule in CHIP_FALLBACK_RULES] == ["near_resistance", "near_support"]
    assert _chip_position_score_current(_feature(price=99, support=80, resistance=100), None) == 54
    assert _chip_position_score_current(_feature(price=82, support=80, resistance=100), None) == 48
    assert _chip_position_score_current(_feature(price=90, support=80, resistance=100), None) == 52
    assert _chip_position_score_current(_feature(price=99, support=99, resistance=100), None) == 54


def test_chip_fallback_ignores_each_unavailable_structural_placeholder() -> None:
    first = _feature(
        price=99,
        support=99,
        resistance=100,
        support_available=False,
        resistance_available=False,
    )
    perturbed = _feature(
        price=99,
        support=1,
        resistance=999,
        support_available=False,
        resistance_available=False,
    )
    assert _chip_position_score_current(first, None) == 52
    assert _chip_position_score_current(first, None) == _chip_position_score_current(perturbed, None)


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


def test_insufficient_chip_placeholder_is_not_a_chip_model() -> None:
    chip = _chip(center_price=100, concentration=35, distribution_available=False)
    assert _chip_position_score_current(_feature(price=100, support=0, resistance=0), chip) == 52


def _analysis(*, change_pct: float):
    return SimpleNamespace(quote=SimpleNamespace(change_pct=change_pct))


def _feature(
    *,
    price: float = 90,
    support: float = 80,
    resistance: float = 120,
    volume_ratio: float = 1.0,
    volume_ratio_available: bool = True,
    data_quality_score: int = 80,
    order_pressure: str = "均衡",
    order_pressure_data_nature: str = "derived",
    ma20: float = 100,
    ma20_available: bool = True,
    support_available: bool = True,
    resistance_available: bool = True,
):
    return SimpleNamespace(
        price=price,
        support=support,
        resistance=resistance,
        volume_ratio=volume_ratio,
        volume_ratio_available=volume_ratio_available,
        data_quality_score=data_quality_score,
        order_pressure=order_pressure,
        order_pressure_data_nature=order_pressure_data_nature,
        ma20=ma20,
        ma20_available=ma20_available,
        support_available=support_available,
        resistance_available=resistance_available,
    )


def _chip(*, center_price: float, concentration: int, distribution_available: bool = True):
    return SimpleNamespace(
        center_price=center_price,
        concentration=concentration,
        distribution_available=distribution_available,
    )
