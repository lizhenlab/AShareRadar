from __future__ import annotations

from datetime import datetime

import pytest

from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_features import build_feature_snapshot
from app.services.research_factor_weights import (
    _adjusted_factor_weight,
    _factor_weight_context,
    _factor_weight_policy,
)
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote


def test_factor_weight_policy_uses_default_profile_without_matching_style() -> None:
    analysis, feature = _factor_weight_inputs(data_quality_score=90)

    profile, adjustments, notes = _factor_weight_policy(analysis, feature)

    assert profile == "常规个股"
    assert adjustments == {}
    assert notes == ["画像只作说明，使用固定方向预算；风险单独约束，数据质量单独披露。"]


def test_factor_weight_policy_prioritizes_large_stable_profile() -> None:
    analysis, feature = _factor_weight_inputs(
        market_cap=600_000_000_000,
        turnover_rate=9.5,
        volume_ratio=2.0,
        data_quality_score=90,
    )

    profile, adjustments, notes = _factor_weight_policy(analysis, feature)

    assert profile == "大市值稳健股"
    assert adjustments == {}
    assert "固定方向预算" in notes[0]


def test_factor_weight_policy_never_applies_low_quality_overlay() -> None:
    analysis, feature = _factor_weight_inputs(
        turnover_rate=8.2,
        volume_ratio=1.0,
        data_quality_score=65,
    )

    profile, adjustments, notes = _factor_weight_policy(analysis, feature)

    assert profile == "高活跃波动股"
    assert adjustments == {}
    assert "固定方向预算" in notes[0]


def test_factor_weight_policy_detects_low_liquidity_profile() -> None:
    analysis, feature = _factor_weight_inputs(
        amount=250_000_000,
        turnover_rate=3.5,
        volume_ratio=1.0,
        data_quality_score=90,
    )

    profile, adjustments, notes = _factor_weight_policy(analysis, feature)

    assert profile == "低流动性个股"
    assert adjustments == {}
    assert "固定方向预算" in notes[0]


@pytest.mark.parametrize(
    ("turnover_rate", "expected_profile"),
    [
        (None, "常规个股"),
        (0, "大市值稳健股"),
        (1.9, "大市值稳健股"),
        (2.0, "常规个股"),
    ],
)
def test_factor_weight_policy_preserves_missing_turnover_and_low_turnover_boundary(
    turnover_rate: float | None,
    expected_profile: str,
) -> None:
    analysis, feature = _factor_weight_inputs(
        amount=3_000_000_000,
        turnover_rate=turnover_rate,
        volume_ratio=1.0,
        data_quality_score=90,
    )

    context = _factor_weight_context(analysis, feature)
    profile, _, _ = _factor_weight_policy(analysis, feature)

    assert context.turnover == turnover_rate
    assert profile == expected_profile


def test_adjusted_factor_weight_clamps_final_weight() -> None:
    assert _adjusted_factor_weight("risk_pressure", 2.0, {"risk_pressure": 2.0}) == 1.8
    assert _adjusted_factor_weight("valuation_anchor", 0.2, {"valuation_anchor": 0.2}) == 0.5
    assert _adjusted_factor_weight("trend_momentum", 0.9, {}) == 0.9


@pytest.mark.parametrize("placeholder", [0.0, 2.0, 99.0])
def test_unavailable_volume_ratio_never_changes_factor_profile(
    placeholder: float,
) -> None:
    analysis, feature = _factor_weight_inputs(
        turnover_rate=4.2,
        volume_ratio=placeholder,
        data_quality_score=90,
    )
    feature = feature.model_copy(update={"volume_ratio_available": False})

    context = _factor_weight_context(analysis, feature)
    profile, adjustments, notes = _factor_weight_policy(analysis, feature)

    assert context.volume_ratio == 0
    assert profile == "常规个股"
    assert adjustments == {}
    assert notes == ["画像只作说明，使用固定方向预算；风险单独约束，数据质量单独披露。"]


@pytest.mark.parametrize("quality", [0, 65, 69, 70, 100])
@pytest.mark.parametrize("profile_fields,expected_profile", [
    ({}, "常规个股"),
    ({"market_cap": 600_000_000_000}, "大市值稳健股"),
    ({"turnover_rate": 8.0}, "高活跃波动股"),
    ({"volume_ratio": 1.6}, "高活跃波动股"),
    ({"amount": 250_000_000}, "低流动性个股"),
])
def test_current_profile_and_quality_never_modify_directional_weights(quality, profile_fields, expected_profile):
    analysis, feature = _factor_weight_inputs(data_quality_score=quality, **profile_fields)
    profile, adjustments, notes = _factor_weight_policy(analysis, feature)
    assert profile == expected_profile
    assert adjustments == {}
    assert "固定方向预算" in "".join(notes)
    assert "提高" not in "".join(notes) and "降低" not in "".join(notes)


@pytest.mark.parametrize("field,low,high", [("turnover_rate", 7.99, 8.0), ("volume_ratio", 1.59, 1.6)])
def test_activity_profile_boundary_changes_only_the_label(field, low, high):
    before = _factor_weight_policy(*_factor_weight_inputs(**{field: low}))
    after = _factor_weight_policy(*_factor_weight_inputs(**{field: high}))
    assert before[0] == "常规个股"
    assert after[0] == "高活跃波动股"
    assert before[1:] == after[1:]


def _factor_weight_inputs(
    *,
    amount: float = 1_300_000_000,
    market_cap: float | None = None,
    turnover_rate: float | None = 4.2,
    volume_ratio: float = 1.0,
    data_quality_score: int = 90,
):
    quote = make_quote(turnover_rate=turnover_rate, market_cap=market_cap).model_copy(update={"amount": amount})
    klines = [
        make_kline(
            date=f"2026-05-{index + 1:02d}",
            close=100 + index * 0.5,
            high=101 + index * 0.5,
            low=99 + index * 0.5,
            volume=1600 + index * 30,
        )
        for index in range(40)
    ]
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)).model_copy(
        update={"score": data_quality_score, "level": "优秀" if data_quality_score >= 80 else "一般"}
    )
    analysis = build_analysis(quote, klines, data_quality=quality)
    feature = build_feature_snapshot(analysis, build_stock_insight_bundle(analysis)).model_copy(
        update={
            "amount": amount,
            "turnover_rate": turnover_rate,
            "volume_ratio": volume_ratio,
            "volume_ratio_available": True,
            "data_quality_score": data_quality_score,
            "data_quality_level": quality.level,
        }
    )
    return analysis, feature
