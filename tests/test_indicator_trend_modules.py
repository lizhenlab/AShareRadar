from __future__ import annotations

import math
import unittest

from app.services.indicator_trend import trend_score_snapshot
from app.services.indicator_trend_components import MOVING_AVERAGE_RULES, VOLUME_SIGNAL_RULES, change_impact, impact_level, turnover_signal, volume_signal
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


if __name__ == "__main__":
    unittest.main()
