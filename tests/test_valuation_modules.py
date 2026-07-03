from __future__ import annotations

import math
import unittest
from datetime import datetime

from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.valuation_anchors import VALUATION_ANCHOR_BANDS, valuation_anchor_label, valuation_percentile_from_history
from app.services.valuation_analysis import build_valuation_analysis
from app.services.valuation_components import (
    PEER_PERCENTILE_DELTA_RULES,
    VALUATION_PERCENTILE_DELTA_RULES,
    peer_percentile_score_delta,
    valuation_percentile_score_delta,
    valuation_summary,
)
from tests.factories import make_kline as _kline
from tests.factories import make_quote as _quote


class ValuationModuleTests(unittest.TestCase):
    def test_missing_valuation_fields_return_low_confidence_summary(self) -> None:
        quote = _quote(pe=None, pb=None, market_cap=None, timestamp="2026-05-13 15:00:00")
        klines = [_kline(date="2026-05-13", close=1300.0, volume=2000.0) for _ in range(80)]
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
        )

        valuation = build_valuation_analysis(analysis)

        self.assertEqual(valuation.summary, "估值字段不足，暂只能做低置信度观察。")
        self.assertIn("PE", valuation.missing_data)
        self.assertIn("PB", valuation.missing_data)
        self.assertIn("总市值", valuation.missing_data)
        self.assertTrue(valuation.evidence)
        self.assertTrue(valuation.watch_points)

    def test_valuation_percentile_score_deltas_keep_risk_direction(self) -> None:
        self.assertEqual([rule.name for rule in VALUATION_PERCENTILE_DELTA_RULES], ["very_high", "high", "very_low", "low"])
        self.assertEqual([rule.name for rule in PEER_PERCENTILE_DELTA_RULES], ["very_high", "high", "very_low", "low"])
        self.assertEqual(valuation_percentile_score_delta(90, 25.0), -10)
        self.assertEqual(valuation_percentile_score_delta(72, 25.0), -5)
        self.assertEqual(valuation_percentile_score_delta(15, 25.0), 6)
        self.assertEqual(valuation_percentile_score_delta(32, 25.0), 3)
        self.assertEqual(valuation_percentile_score_delta(40, -1.0), -10)
        self.assertEqual(valuation_percentile_score_delta(-5, 25.0), 0)
        self.assertEqual(valuation_percentile_score_delta(105, 25.0), 0)
        self.assertEqual(valuation_percentile_score_delta(40, True), -10)
        self.assertEqual(peer_percentile_score_delta(90, 25.0), -8)
        self.assertEqual(peer_percentile_score_delta(72, 25.0), -4)
        self.assertEqual(peer_percentile_score_delta(15, 25.0), 5)
        self.assertEqual(peer_percentile_score_delta(32, 25.0), 2)
        self.assertEqual(peer_percentile_score_delta(-1, 25.0), 0)
        self.assertEqual(peer_percentile_score_delta(101, 25.0), 0)

    def test_summary_distinguishes_missing_enrichment_from_missing_core_fields(self) -> None:
        enrichment_missing = ["PE历史分位", "PB历史分位", "同行PE分位", "同行PB分位"]
        many_enrichment_missing = ["价格历史分位", "PE历史分位", "PB历史分位", "同行PE分位", "同行PB分位", "行业估值分位"]
        core_missing = ["PE", "PB", "同行PE分位"]

        self.assertEqual(valuation_summary(57, enrichment_missing), "估值处在中性区间，重点看业绩和行业背景能否配合。")
        self.assertEqual(valuation_summary(57, many_enrichment_missing), "估值处在中性区间，重点看业绩和行业背景能否配合。")
        self.assertEqual(valuation_summary(57, core_missing), "估值字段不足，暂只能做低置信度观察。")

    def test_valuation_history_percentile_skips_malformed_values(self) -> None:
        quote = _quote(pe=25.0, timestamp="2026-05-13 15:00:00")
        klines = [_kline(date="2026-05-13", close=1300.0, volume=2000.0) for _ in range(80)]
        history = [{"pe": 10 + index, "quote_timestamp": f"2026-04-{index + 1:02d} 15:00:00"} for index in range(30)]
        history.extend(
            [
                {"pe": "bad", "quote_timestamp": "2026-05-01 15:00:00"},
                {"pe": None, "quote_timestamp": "2026-05-02 15:00:00"},
                {"pe": -1, "quote_timestamp": "2026-05-03 15:00:00"},
            ]
        )
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
            quote_history=history,
        )

        self.assertEqual(valuation_percentile_from_history(analysis, "pe"), 53.3)

    def test_valuation_history_percentile_uses_latest_snapshot_per_day(self) -> None:
        quote = _quote(pe=20.0, timestamp="2026-05-13 15:00:00")
        klines = [_kline(date="2026-05-13", close=1300.0, volume=2000.0) for _ in range(80)]
        history = [
            {"pe": 30.0, "quote_timestamp": f"2026-04-{index + 1:02d} 15:00:00"}
            for index in range(29)
        ]
        history.extend(
            [
                {
                    "pe": 40.0,
                    "quote_timestamp": "2026-05-01 15:00:00",
                    "fetched_at": "2026-05-01 15:01:00",
                },
                {
                    "pe": 10.0,
                    "quote_timestamp": "2026-05-01 15:00:00",
                    "fetched_at": "2026-05-01 10:00:00",
                },
            ]
        )
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
            quote_history=history,
        )

        self.assertEqual(valuation_percentile_from_history(analysis, "pe"), 0.0)

    def test_current_valuation_summary_hides_non_finite_raw_values(self) -> None:
        quote = _quote(pe=math.inf, pb=math.nan, market_cap=100_000_000, timestamp="2026-05-13 15:00:00")
        klines = [_kline(date="2026-05-13", close=1300.0, volume=2000.0) for _ in range(80)]
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
        )

        valuation = build_valuation_analysis(analysis)
        visible_text = "；".join([*valuation.evidence, *valuation.watch_points])

        self.assertIn("PE 字段异常", visible_text)
        self.assertIn("PB 字段异常", visible_text)
        self.assertNotIn("PE inf", visible_text)
        self.assertNotIn("PB nan", visible_text)

    def test_valuation_anchor_label_uses_ordered_bands_and_valuation_priority(self) -> None:
        self.assertEqual([rule.name for rule in VALUATION_ANCHOR_BANDS], ["high", "elevated", "low", "discount"])

        self.assertEqual(valuation_anchor_label(10, pe_percentile=90), "高位估值锚")
        self.assertEqual(valuation_anchor_label(90, pe_percentile=65), "偏高估值锚")
        self.assertEqual(valuation_anchor_label(90, pe_percentile=20), "低位估值锚")
        self.assertEqual(valuation_anchor_label(90, pe_percentile=35), "偏低估值锚")
        self.assertEqual(valuation_anchor_label(90, pe_percentile=50), "中性估值锚")

    def test_valuation_anchor_label_falls_back_to_price_position_or_pending(self) -> None:
        self.assertEqual(valuation_anchor_label(85), "高位价格位置锚")
        self.assertEqual(valuation_anchor_label(34.9), "偏低价格位置锚")
        self.assertEqual(valuation_anchor_label(-1), "历史锚待确认")
        self.assertEqual(valuation_anchor_label(90, pe_percentile=120), "高位价格位置锚")
        self.assertEqual(valuation_anchor_label(None), "历史锚待确认")


if __name__ == "__main__":
    unittest.main()
