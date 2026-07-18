from __future__ import annotations

import unittest
from datetime import datetime

from app.models.schemas import AnalysisResult, OrderBook, OrderBookLevel, Quote
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.stock_activity import build_fund_flow_analysis, build_order_pressure
from tests.factories import make_kline, make_quote


NOW = datetime(2026, 5, 13, 16, 0, 0)


class StockActivityModuleTests(unittest.TestCase):
    def test_fund_flow_negative_amount_is_not_available(self) -> None:
        quote = make_quote(turnover_rate=-1.0, timestamp="2026-05-13 15:00:00").model_copy(update={"amount": -1000.0})
        fund_flow = build_fund_flow_analysis(_analysis(quote=quote))

        self.assertFalse(fund_flow.available)
        self.assertEqual(fund_flow.data_nature, "derived")
        self.assertEqual(fund_flow.windows[0].label, "今日量价热度")
        self.assertIn("derived（衍生）", fund_flow.notes[0])
        self.assertIn("不是真实资金流", fund_flow.notes[0])

    def test_fund_flow_low_quality_adds_downgrade_note(self) -> None:
        quote = make_quote(source="本地演示数据", timestamp="2026-05-13 15:00:00")
        fund_flow = build_fund_flow_analysis(_analysis(quote=quote))

        self.assertTrue(any("评分已降权" in note for note in fund_flow.notes))

    def test_fund_flow_relation_reports_positive_volume_confirmation(self) -> None:
        quote = make_quote(change_pct=2.0, timestamp="2026-05-13 15:00:00").model_copy(update={"volume": 1800.0})
        klines = [make_kline(close=100.0, volume=1000.0, date=f"2026-05-{day:02d}") for day in range(8, 13)]

        fund_flow = build_fund_flow_analysis(_analysis(quote=quote, klines=klines))

        self.assertEqual(fund_flow.price_volume_relation, "量价配合偏积极。")

    def test_fund_flow_relation_falls_back_to_kline_volume_when_quote_volume_is_invalid(self) -> None:
        quote = make_quote(change_pct=2.0, timestamp="2026-05-13 15:00:00").model_copy(update={"volume": 0.0})
        klines = [make_kline(close=100.0, volume=1000.0, date=f"2026-05-{day:02d}") for day in range(8, 13)]
        klines.append(make_kline(close=102.0, volume=1800.0, date="2026-05-13"))

        fund_flow = build_fund_flow_analysis(_analysis(quote=quote, klines=klines))

        self.assertEqual(fund_flow.price_volume_relation, "量价配合偏积极。")

    def test_fund_flow_relation_uses_kline_volume_when_quote_timestamp_is_stale(self) -> None:
        quote = make_quote(change_pct=2.0, timestamp="2026-05-12 15:00:00").model_copy(update={"volume": 100.0})
        klines = [make_kline(close=100.0, volume=1000.0, date=f"2026-05-{day:02d}") for day in range(8, 13)]
        klines.append(make_kline(close=102.0, volume=1800.0, date="2026-05-13"))

        fund_flow = build_fund_flow_analysis(_analysis(quote=quote, klines=klines))

        self.assertEqual(fund_flow.price_volume_relation, "量价配合偏积极。")

    def test_fund_flow_non_finite_change_pct_keeps_price_volume_relation_neutral(self) -> None:
        quote = make_quote(change_pct=0.0, timestamp="2026-05-13 15:00:00").model_copy(
            update={"change_pct": float("inf"), "volume": 1800.0}
        )
        klines = [make_kline(close=100.0, volume=1000.0, date=f"2026-05-{day:02d}") for day in range(8, 13)]

        fund_flow = build_fund_flow_analysis(_analysis(quote=quote, klines=klines))

        self.assertEqual(fund_flow.price_volume_relation, "量价关系中性，等待更明确方向。")

    def test_fund_flow_exact_price_change_edges_keep_relation_neutral(self) -> None:
        klines = [make_kline(close=100.0, volume=1000.0, date=f"2026-05-{day:02d}") for day in range(8, 13)]

        for change_pct in (1.0, -1.0):
            with self.subTest(change_pct=change_pct):
                quote = make_quote(change_pct=change_pct, timestamp="2026-05-13 15:00:00").model_copy(update={"volume": 1800.0})
                fund_flow = build_fund_flow_analysis(_analysis(quote=quote, klines=klines))

                self.assertEqual(fund_flow.price_volume_relation, "量价关系中性，等待更明确方向。")

    def test_fund_flow_invalid_turnover_is_neutral_not_zero_turnover(self) -> None:
        klines = [make_kline(close=100.0, volume=1000.0, date=f"2026-05-{day:02d}") for day in range(8, 13)]
        base_quote = make_quote(change_pct=0.0, timestamp="2026-05-13 15:00:00").model_copy(update={"volume": 1000.0})
        missing_turnover = build_fund_flow_analysis(_analysis(quote=base_quote.model_copy(update={"turnover_rate": None}), klines=klines))
        zero_turnover = build_fund_flow_analysis(_analysis(quote=base_quote.model_copy(update={"turnover_rate": 0.0}), klines=klines))

        for turnover_rate in (-1.0, float("inf")):
            with self.subTest(turnover_rate=turnover_rate):
                fund_flow = build_fund_flow_analysis(_analysis(quote=base_quote.model_copy(update={"turnover_rate": turnover_rate}), klines=klines))

                self.assertEqual(fund_flow.overall_score, missing_turnover.overall_score)
                self.assertGreater(fund_flow.overall_score, zero_turnover.overall_score)

    def test_fund_flow_zero_volume_baseline_keeps_relation_neutral(self) -> None:
        quote = make_quote(change_pct=2.0, timestamp="2026-05-13 15:00:00").model_copy(update={"volume": 1800.0})
        klines = [make_kline(close=100.0, volume=0.0, date=f"2026-05-{day:02d}") for day in range(8, 13)]

        fund_flow = build_fund_flow_analysis(_analysis(quote=quote, klines=klines))

        self.assertEqual(fund_flow.price_volume_relation, "量价关系中性，等待更明确方向。")

    def test_order_book_pressure_reports_strong_bid_side(self) -> None:
        analysis = _analysis()
        pressure = build_order_pressure(
            analysis,
            order_book=_order_book(
                bid=[(99.9, 30_000), (99.8, 10_000)],
                ask=[(100.1, 10_000), (100.2, 8_000)],
            ),
        )

        self.assertTrue(pressure.available)
        self.assertEqual(pressure.data_nature, "observed")
        self.assertEqual(pressure.pressure_level, "买盘偏强")
        self.assertEqual(pressure.bid_ask_ratio, 2.22)
        self.assertEqual(pressure.spread_pct, 0.2)
        self.assertIn("买卖盘金额比约 2.22", pressure.summary)

    def test_zero_bid_amount_is_sold_pressure_not_missing_depth(self) -> None:
        analysis = _analysis()
        pressure = build_order_pressure(
            analysis,
            order_book=_order_book(
                bid=[(99.9, 0)],
                ask=[(100.1, 10_000)],
            ),
        )

        self.assertEqual(pressure.pressure_level, "卖压偏强")
        self.assertEqual(pressure.bid_ask_ratio, 0.0)
        self.assertIn("买卖盘金额比约 0.00", pressure.summary)
        self.assertNotIn("盘口深度不足", pressure.summary)

    def test_order_book_without_ask_depth_keeps_insufficient_depth_summary(self) -> None:
        analysis = _analysis()
        pressure = build_order_pressure(
            analysis,
            order_book=_order_book(
                bid=[(99.9, 10_000)],
                ask=[],
            ),
        )

        self.assertEqual(pressure.pressure_level, "盘口均衡")
        self.assertIsNone(pressure.bid_ask_ratio)
        self.assertEqual(pressure.summary, "盘口深度不足，暂不能判断买卖盘强弱。")

    def test_empty_order_book_depth_keeps_stable_insufficient_depth_summary(self) -> None:
        analysis = _analysis()
        pressure = build_order_pressure(analysis, order_book=_order_book(bid=[], ask=[]))

        self.assertTrue(pressure.available)
        self.assertEqual(pressure.pressure_level, "盘口均衡")
        self.assertIsNone(pressure.bid_ask_ratio)
        self.assertIsNone(pressure.spread_pct)
        self.assertEqual(pressure.bid_amount, 0.0)
        self.assertEqual(pressure.ask_amount, 0.0)
        self.assertEqual(pressure.summary, "盘口深度不足，暂不能判断买卖盘强弱。")

    def test_crossed_order_book_does_not_report_negative_spread(self) -> None:
        analysis = _analysis()
        pressure = build_order_pressure(
            analysis,
            order_book=_order_book(
                bid=[(100.2, 10_000)],
                ask=[(100.1, 10_000)],
            ),
        )

        self.assertIsNone(pressure.spread_pct)
        self.assertEqual(pressure.bid_ask_ratio, 1.0)

    def test_order_book_ratio_threshold_edges_remain_neutral(self) -> None:
        analysis = _analysis()

        for bid_volume, expected_ratio in ((1250, 1.25), (800, 0.8)):
            with self.subTest(expected_ratio=expected_ratio):
                pressure = build_order_pressure(
                    analysis,
                    order_book=_order_book(
                        bid=[(100.0, bid_volume)],
                        ask=[(100.0, 1000)],
                    ),
                )

                self.assertEqual(pressure.pressure_level, "盘口均衡")
                self.assertEqual(pressure.bid_ask_ratio, expected_ratio)

    def test_order_book_ignores_invalid_depth_levels(self) -> None:
        analysis = _analysis()
        pressure = build_order_pressure(
            analysis,
            order_book=_order_book(
                bid=[(-99.9, 10_000), (100.0, 1_000)],
                ask=[(100.1, -5_000), (100.2, 500)],
            ),
        )

        self.assertEqual(pressure.bid_amount, 100000.0)
        self.assertEqual(pressure.ask_amount, 50100.0)
        self.assertEqual(pressure.bid_ask_ratio, 2.0)
        self.assertEqual(pressure.pressure_level, "买盘偏强")

    def test_order_book_spread_skips_non_finite_top_levels(self) -> None:
        analysis = _analysis()
        order_book = _order_book(
            bid=[(99.8, 10_000), (99.9, 1_000)],
            ask=[(100.2, 10_000), (100.1, 1_000)],
        )
        order_book = order_book.model_copy(
            update={
                "bid": [order_book.bid[0].model_copy(update={"price": float("nan")}), order_book.bid[1]],
                "ask": [order_book.ask[0].model_copy(update={"price": float("inf")}), order_book.ask[1]],
            }
        )
        pressure = build_order_pressure(
            analysis,
            order_book=order_book,
        )

        self.assertEqual(pressure.bid_amount, 99900.0)
        self.assertEqual(pressure.ask_amount, 100100.0)
        self.assertEqual(pressure.spread_pct, 0.2)
        self.assertEqual(pressure.pressure_level, "盘口均衡")

    def test_missing_order_book_uses_range_pressure_fallback(self) -> None:
        quote = make_quote(price=99.0, prev_close=100.0, high=100.0, low=94.0, change_pct=-1.0, timestamp="2026-05-13 15:00:00")
        pressure = build_order_pressure(_analysis(quote=quote), order_book_error="Futu OpenD 未连接")

        self.assertFalse(pressure.available)
        self.assertEqual(pressure.data_nature, "estimated")
        self.assertEqual(pressure.pressure_level, "上方卖压待消化")
        self.assertIn("日内振幅约 6.06%", pressure.summary)
        self.assertIn("Futu OpenD 未连接", pressure.notes)

    def test_missing_order_book_and_invalid_range_are_explicitly_unavailable(self) -> None:
        quote = make_quote(timestamp="2026-05-13 15:00:00").model_copy(
            update={"price": float("nan"), "high": float("inf"), "low": float("nan")}
        )

        pressure = build_order_pressure(_analysis(quote=quote), order_book_error="盘口源不可用")

        self.assertFalse(pressure.available)
        self.assertEqual(pressure.data_nature, "unavailable")
        self.assertEqual(pressure.pressure_level, "订单压力不可用")
        self.assertNotIn("0.00%", pressure.summary)
        self.assertIn("unavailable（不可用）", pressure.notes[0])

    def test_missing_proxy_inputs_are_unavailable_and_neutral_not_zero(self) -> None:
        quote = make_quote(timestamp="2026-05-13 15:00:00").model_copy(
            update={"amount": float("nan"), "turnover_rate": None, "change_pct": float("nan")}
        )

        fund_flow = build_fund_flow_analysis(_analysis(quote=quote, klines=[make_kline(date="2026-05-13")]))

        self.assertEqual(fund_flow.data_nature, "unavailable")
        self.assertEqual(fund_flow.overall_score, 50)
        self.assertEqual(fund_flow.level, "不可用")
        self.assertIn("方向不可用", fund_flow.price_volume_relation)

    def test_low_quality_data_downgrades_order_book_and_range_pressure(self) -> None:
        quote = make_quote(source="本地演示数据", timestamp="2026-05-13 15:00:00")
        analysis = _analysis(quote=quote)

        realtime = build_order_pressure(analysis, order_book=_order_book(bid=[(99.9, 30_000)], ask=[(100.1, 10_000)]))
        fallback = build_order_pressure(analysis)

        self.assertTrue(realtime.pressure_level.endswith("（降权）"))
        self.assertIn("低可信等级参考", realtime.notes[-1])
        self.assertTrue(fallback.pressure_level.endswith("（降权）"))
        self.assertIn("估算结论已降权", fallback.notes[-1])


def _analysis(*, quote: Quote | None = None, klines: list | None = None) -> AnalysisResult:
    quote = quote or make_quote(price=100.0, prev_close=99.0, high=101.0, low=98.0, change_pct=1.01, timestamp="2026-05-13 15:00:00")
    klines = klines or [make_kline(close=100.0, volume=1000.0, date="2026-05-13") for _ in range(80)]
    return build_analysis(quote, klines, data_quality=build_data_quality(quote, klines, now=NOW))


def _order_book(*, bid: list[tuple[float, float]], ask: list[tuple[float, float]]) -> OrderBook:
    return OrderBook(
        symbol="600519.SH",
        code="600519",
        market="SH",
        bid=[OrderBookLevel(price=price, volume=volume) for price, volume in bid],
        ask=[OrderBookLevel(price=price, volume=volume) for price, volume in ask],
        source="测试盘口",
        updated_at="2026-05-13 10:00:00",
    )


if __name__ == "__main__":
    unittest.main()
