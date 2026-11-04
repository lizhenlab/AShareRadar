from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.services.indicator_trend import trend_score_snapshot
from app.services.indicator_trend_components import (
    MARKET_SCAN_TURNOVER_KNOTS,
    TREND_SCORE_ALGORITHM_VERSION,
    TREND_SCORE_LEGACY_ALGORITHM_VERSION,
    TREND_SCORE_MARKET_SCAN_ALGORITHM_VERSION,
    bounded_turnover_signal,
    build_trend_context,
    canonical_price_change_pct,
    position_contributions,
    turnover_contribution,
    turnover_signal,
)
from tests.factories import make_kline, make_quote


def _rows(*, high: float = 10.1, low: float = 9.9):
    return [
        make_kline(
            close=10,
            high=high,
            low=low,
            volume=4_000_000 if index >= 85 else 1_000_000,
            date=(date(2026, 1, 1) + timedelta(days=index)).isoformat(),
        ).model_copy(update={"open": 10.0})
        for index in range(90)
    ]


def _quote(**updates):
    return make_quote(price=10, prev_close=10, high=10.1, low=9.9, change_pct=0, turnover_rate=1).model_copy(update=updates)


def _contributions(quote, *, algorithm_version=TREND_SCORE_MARKET_SCAN_ALGORITHM_VERSION):
    score, _, contributions = trend_score_snapshot(quote, _rows(), algorithm_version=algorithm_version)
    return score, {item.name: item for item in contributions}


@pytest.mark.parametrize("reported_pct", [-0.29, 0.0, 0.29])
def test_opt_in_direction_uses_canonical_price_pair(reported_pct: float) -> None:
    score, contributions = _contributions(_quote(change_pct=reported_pct))

    assert score == 50
    assert contributions["日内涨跌"].impact == 0
    assert contributions["量价确认"].impact == 0
    assert contributions["窄幅区间"].impact == 0


@pytest.mark.parametrize("price,reported_pct,expected_volume", [(10.02, -0.09, 6), (9.98, 0.09, -7)])
def test_opt_in_volume_follows_true_return_when_reported_sign_disagrees(price, reported_pct, expected_volume) -> None:
    _, contributions = _contributions(_quote(price=price, change_pct=reported_pct))

    assert contributions["量价确认"].impact == expected_volume


@pytest.mark.parametrize("price,reported_pct,expected_impact", [(10.301, 2.99, 10), (9.699, -2.99, -12)])
def test_opt_in_daily_price_rule_uses_the_same_canonical_return(price, reported_pct, expected_impact) -> None:
    _, contributions = _contributions(_quote(price=price, change_pct=reported_pct))

    assert contributions["日内涨跌"].impact == expected_impact


@pytest.mark.parametrize("scale", [1e-6, 1, 1e6])
def test_canonical_return_is_stable_at_thresholds_and_under_rescaling(scale) -> None:
    assert canonical_price_change_pct(10.1 * scale, 10 * scale) == 1.0
    assert canonical_price_change_pct(9.7 * scale, 10 * scale) == -3.0
    assert canonical_price_change_pct(10 * scale, 10 * scale) == 0.0
    assert math.copysign(1, canonical_price_change_pct(10 * scale, 10 * scale)) == 1


@pytest.mark.parametrize("price,prev_close", [(0, 10), (-1, 10), (10, 0), (10, -1), (10, None), (math.inf, 10), (10, math.inf), (math.nan, 10), (10, math.nan), (1e308, 1e-308)])
def test_canonical_return_rejects_invalid_or_overflowing_prices(price, prev_close) -> None:
    with pytest.raises(ValueError, match="canonical return"):
        canonical_price_change_pct(price, prev_close)


@pytest.mark.parametrize("turnover,expected", MARKET_SCAN_TURNOVER_KNOTS)
def test_bounded_turnover_retains_frozen_knot_values(turnover, expected) -> None:
    assert bounded_turnover_signal(turnover)[0] == expected
    if turnover > 0:
        assert bounded_turnover_signal(turnover - 1e-7)[0] == expected
    assert bounded_turnover_signal(turnover + 1e-7)[0] == expected


@pytest.mark.parametrize("turnover,expected", [(1.5, 4), (9, 4), (12.5, 0), (17.5, -3), (20.0001, -5), (1e308, -5)])
def test_bounded_turnover_interpolates_and_rounds_negative_half_away(turnover, expected) -> None:
    assert bounded_turnover_signal(turnover)[0] == expected


def test_bounded_turnover_has_only_one_point_quantization_steps() -> None:
    impacts = [bounded_turnover_signal(index / 1000)[0] for index in range(22_001)]
    assert min(impacts) == -5
    assert max(impacts) == 8
    assert max(abs(right - left) for left, right in zip(impacts, impacts[1:], strict=False)) == 1
    assert impacts[1000:2001] == sorted(impacts[1000:2001])
    assert impacts[8000:10001] == sorted(impacts[8000:10001], reverse=True)
    assert impacts[15000:20001] == sorted(impacts[15000:20001], reverse=True)


@pytest.mark.parametrize("turnover", [-1, -1e-10, math.nan, math.inf, -math.inf])
def test_bounded_turnover_rejects_invalid_values(turnover) -> None:
    with pytest.raises(ValueError, match="turnover rate"):
        bounded_turnover_signal(turnover)


def test_zero_turnover_is_valid_and_none_is_missing_only_in_opt_in_rules() -> None:
    zero = turnover_contribution(build_trend_context(_quote(turnover_rate=0), _rows(), algorithm_version=TREND_SCORE_MARKET_SCAN_ALGORITHM_VERSION))
    missing = turnover_contribution(build_trend_context(_quote(turnover_rate=None), _rows(), algorithm_version=TREND_SCORE_MARKET_SCAN_ALGORITHM_VERSION))
    old_zero = turnover_contribution(build_trend_context(_quote(turnover_rate=0), _rows()))

    assert zero.impact == missing.impact == old_zero.impact == 0
    assert "缺少" not in zero.reason
    assert "缺少" in missing.reason
    assert "缺少" in old_zero.reason


@pytest.mark.parametrize("high,low,price,expected", [(10.1, 9.9, 10, [("窄幅区间", 0)]), (10, 10, 10, [("窄幅区间", 0)]), (12, 9, 11.9, [("接近20日高位", 12)]), (12, 9, 9.1, [("接近20日低位", -10)]), (12, 9, 10.5, [])])
def test_opt_in_position_is_neutral_only_for_overlap(high, low, price, expected) -> None:
    context = build_trend_context(_quote(price=price), _rows(), algorithm_version=TREND_SCORE_MARKET_SCAN_ALGORITHM_VERSION)
    contributions = position_contributions(replace(context, recent_high=high, recent_low=low))
    assert [(item.name, item.impact) for item in contributions] == expected


def test_opt_in_position_includes_the_exact_shared_boundary() -> None:
    # 1.03 / 0.985 is the exact range ratio at which the old bands meet.
    high, low = 1.03, 0.985
    boundary = high * 0.985
    context = build_trend_context(_quote(price=boundary), _rows(), algorithm_version=TREND_SCORE_MARKET_SCAN_ALGORITHM_VERSION)
    context = replace(context, recent_high=high, recent_low=low)
    assert [(item.name, item.impact) for item in position_contributions(context)] == [("窄幅区间", 0)]
    assert position_contributions(replace(context, quote=_quote(price=boundary - 1e-8)))[0].impact == -10
    assert position_contributions(replace(context, quote=_quote(price=boundary + 1e-8)))[0].impact == 12


@pytest.mark.parametrize("algorithm_version", [TREND_SCORE_LEGACY_ALGORITHM_VERSION, TREND_SCORE_ALGORITHM_VERSION])
@pytest.mark.parametrize("reported_pct,expected_score,expected_volume", [(-0.29, 45, -7), (0, 52, 0), (0.29, 58, 6)])
def test_frozen_algorithms_preserve_reported_direction_and_overlapping_positions(algorithm_version, reported_pct, expected_score, expected_volume) -> None:
    score, contributions = _contributions(_quote(change_pct=reported_pct), algorithm_version=algorithm_version)

    assert score == expected_score
    assert contributions["量价确认"].impact == expected_volume
    assert contributions["接近20日高位"].impact == 12
    assert contributions["接近20日低位"].impact == -10
    assert "窄幅区间" not in contributions


@pytest.mark.parametrize("turnover,expected", [(0, 0), (1.9999, 0), (2, 8), (8, 8), (8.0001, 0), (15, 0), (15.0001, -5)])
def test_legacy_turnover_helper_preserves_all_original_boundaries(turnover, expected) -> None:
    assert turnover_signal(turnover)[0] == expected


def test_default_algorithm_accepts_price_inputs_without_prev_close() -> None:
    quote = SimpleNamespace(price=10.0, change_pct=0.29, turnover_rate=1.0)
    default_score, _, _ = trend_score_snapshot(quote, _rows())
    assert default_score == 58
    with pytest.raises(ValueError, match="canonical return"):
        build_trend_context(quote, _rows(), algorithm_version=TREND_SCORE_MARKET_SCAN_ALGORITHM_VERSION)
