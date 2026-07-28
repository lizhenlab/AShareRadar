from __future__ import annotations

import math
import unittest

from app.services.indicator_trend import trend_score_snapshot
from app.services.indicator_trend_components import (
    MOVING_AVERAGE_RULES,
    RELATIVE_FULL_EFFECT_PCT,
    RELATIVE_NEUTRAL_BAND_PCT,
    VOLUME_SIGNAL_RULES,
    TrendContext,
    change_impact,
    continuous_relative_impact,
    impact_level,
    moving_average_contributions,
    slope_contributions,
    turnover_signal,
    volume_signal,
)
from tests.factories import make_kline as _kline
from tests.factories import make_quote as _quote


class IndicatorTrendModuleTests(unittest.TestCase):
    def test_short_kline_sample_returns_neutral_data_contribution(self) -> None:
        score, label, contributions = trend_score_snapshot(_quote(), [_kline() for _ in range(10)])

        self.assertEqual(score, 50)
        self.assertEqual(label, "数据不足")
        self.assertEqual(contributions[0].name, "K线样本不足")
        self.assertEqual(contributions[0].impact, 0)

    def test_trend_contribution_order_is_stable_for_full_sample(self) -> None:
        klines = [
            _kline(close=100 + index, high=101 + index, low=99 + index, volume=1000 + index * 80)
            for index in range(30)
        ]
        quote = _quote(price=131.0, prev_close=128.0, high=132.0, low=127.0, change_pct=2.34, turnover_rate=4.5)

        score, label, contributions = trend_score_snapshot(quote, klines)

        self.assertGreaterEqual(score, 65)
        self.assertIn(label, {"偏强震荡", "强势上行"})
        self.assertEqual(
            [item.name for item in contributions[:7]],
            ["现价与5日线", "短线均线排列", "波段均线排列", "短线斜率", "波段斜率", "日内涨跌", "接近20日高位"],
        )
        self.assertEqual(contributions[-2].name, "换手率")
        self.assertEqual(contributions[-1].name, "量价确认")

    def test_trend_score_ignores_non_finite_or_out_of_bounds_kline_rows(self) -> None:
        clean = [
            _kline(close=100 + index, high=101 + index, low=99 + index, volume=1000 + index * 80)
            for index in range(30)
        ]
        dirty = [
            *clean,
            _kline(close=200, high=201, low=199, volume=2000).model_copy(update={"high": math.inf}),
            _kline(close=210, high=211, low=209, volume=2000).model_copy(update={"open": 230}),
        ]
        quote = _quote(price=131.0, prev_close=128.0, high=132.0, low=127.0, change_pct=2.34, turnover_rate=4.5)

        clean_score, clean_label, clean_contributions = trend_score_snapshot(quote, clean)
        dirty_score, dirty_label, dirty_contributions = trend_score_snapshot(quote, dirty)

        self.assertEqual(dirty_score, clean_score)
        self.assertEqual(dirty_label, clean_label)
        self.assertEqual([item.reason for item in dirty_contributions], [item.reason for item in clean_contributions])

    def test_signal_threshold_helpers_keep_existing_direction(self) -> None:
        self.assertEqual([rule.name for rule in MOVING_AVERAGE_RULES], ["现价与5日线", "短线均线排列", "波段均线排列"])
        self.assertEqual(
            [rule.name for rule in VOLUME_SIGNAL_RULES],
            ["positive_volume_expansion", "negative_volume_expansion", "low_volume_large_move"],
        )
        self.assertEqual(change_impact(3.1), 10)
        self.assertEqual(change_impact(-3.1), -12)
        self.assertEqual(turnover_signal(4.0)[0], 8)
        self.assertEqual(turnover_signal(16.0)[0], -5)
        self.assertEqual(volume_signal(2.0, 1.3)[0], 6)
        self.assertEqual(volume_signal(-2.0, 1.3)[0], -7)
        self.assertEqual(volume_signal(2.1, 0.64)[0], -4)
        self.assertEqual(volume_signal(2.0, 0.64)[0], 0)
        self.assertEqual(impact_level(-8), "风险")

    def test_equal_moving_averages_and_slopes_are_neutral(self) -> None:
        context = _trend_context()

        moving_average_impacts = [item.impact for item in moving_average_contributions(context)]
        slope_impacts = [item.impact for item in slope_contributions(context)]

        self.assertEqual(moving_average_impacts, [0, 0, 0])
        self.assertEqual(slope_impacts, [0, 0])
        self.assertTrue(all("+0.00%" in item.reason for item in moving_average_contributions(context)))

    def test_tiny_perturbations_inside_neutral_band_do_not_change_score(self) -> None:
        klines = [_kline(close=100, high=101, low=99, volume=1000) for _ in range(30)]
        deltas = [-1e-10, 0, 1e-10]
        snapshots = []
        for delta in deltas:
            perturbed_klines = [*klines[:-1], klines[-1].model_copy(update={"close": 100 + delta})]
            snapshots.append(
                trend_score_snapshot(_quote(price=100 + delta, prev_close=100, high=101, low=99, change_pct=0), perturbed_klines)
            )

        self.assertEqual(len({score for score, _, _ in snapshots}), 1)
        self.assertEqual(
            [[item.impact for item in contributions[:5]] for _, _, contributions in snapshots],
            [[0, 0, 0, 0, 0]] * 3,
        )

    def test_relative_impact_is_monotonic_and_keeps_directional_caps(self) -> None:
        values = [95.0, 98.0, 99.5, 100.0, 100.5, 102.0, 105.0]
        impacts = [continuous_relative_impact(value, 100, positive_impact=8, negative_impact=-8)[0] for value in values]

        self.assertEqual(impacts, sorted(impacts))
        self.assertEqual(impacts[0], -8)
        self.assertEqual(impacts[-1], 8)
        self.assertEqual(impacts[3], 0)

    def test_slope_impacts_are_monotonic_and_bounded(self) -> None:
        short_impacts = [slope_contributions(_trend_context(ma5=value))[0].impact for value in [95, 98, 99.5, 100, 100.5, 102, 105]]
        wave_impacts = [slope_contributions(_trend_context(ma20=value))[1].impact for value in [95, 98, 99.5, 100, 100.5, 102, 105]]

        self.assertEqual(short_impacts, sorted(short_impacts))
        self.assertEqual(wave_impacts, sorted(wave_impacts))
        self.assertEqual((short_impacts[0], short_impacts[-1]), (-5, 7))
        self.assertEqual((wave_impacts[0], wave_impacts[-1]), (-6, 6))

    def test_relative_impact_threshold_and_invalid_reference_boundaries(self) -> None:
        neutral_up = continuous_relative_impact(100 * (1 + RELATIVE_NEUTRAL_BAND_PCT / 100), 100, positive_impact=8, negative_impact=-8)
        neutral_down = continuous_relative_impact(100 * (1 - RELATIVE_NEUTRAL_BAND_PCT / 100), 100, positive_impact=8, negative_impact=-8)
        full_up = continuous_relative_impact(100 * (1 + RELATIVE_FULL_EFFECT_PCT / 100), 100, positive_impact=8, negative_impact=-8)
        full_down = continuous_relative_impact(100 * (1 - RELATIVE_FULL_EFFECT_PCT / 100), 100, positive_impact=8, negative_impact=-8)

        self.assertEqual((neutral_up[0], neutral_down[0]), (0, 0))
        self.assertEqual((full_up[0], full_down[0]), (8, -8))
        self.assertEqual(continuous_relative_impact(100, 0, positive_impact=8, negative_impact=-8), (0, None))
        self.assertEqual(continuous_relative_impact(math.inf, 100, positive_impact=8, negative_impact=-8), (0, None))
        with self.assertRaisesRegex(ValueError, "0 <= neutral < full effect"):
            continuous_relative_impact(100, 100, positive_impact=8, negative_impact=-8, neutral_band_pct=1, full_effect_pct=1)

    def test_trend_score_soft_clip_preserves_order_without_saturating_boundaries(self) -> None:
        rising = [_kline(close=100 + index * 2, high=101 + index * 2, low=99 + index * 2, volume=1000) for index in range(30)]
        falling = [_kline(close=160 - index * 2, high=161 - index * 2, low=159 - index * 2, volume=1000) for index in range(30)]

        strong_score = trend_score_snapshot(_quote(price=165, prev_close=158, high=166, low=157, change_pct=4, turnover_rate=4), rising)[0]
        weak_score = trend_score_snapshot(_quote(price=95, prev_close=102, high=103, low=94, change_pct=-4, turnover_rate=16), falling)[0]

        self.assertGreater(strong_score, 90)
        self.assertLess(weak_score, 10)
        self.assertLess(strong_score, 100)
        self.assertGreater(weak_score, 0)


def _trend_context(*, price: float = 100, ma5: float = 100, ma10: float = 100, ma20: float = 100) -> TrendContext:
    return TrendContext(
        quote=_quote(price=price, prev_close=100, high=max(price, 100), low=min(price, 100), change_pct=0),
        klines=[],
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        prev_ma5=100,
        prev_ma20=100,
        recent_high=100,
        recent_low=100,
        volume_ratio=1,
    )


if __name__ == "__main__":
    unittest.main()
