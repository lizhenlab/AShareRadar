from __future__ import annotations

import unittest
from datetime import datetime

from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.financial_health import build_financial_health
from app.services.financial_health_components import liquidity_metric_value
from tests.factories import make_kline as _kline
from tests.factories import make_quote as _quote
from tests.factories import make_stock_info as _stock_info


class FinancialHealthModuleTests(unittest.TestCase):
    def test_missing_core_quote_fields_are_reported_in_missing_data(self) -> None:
        quote = _quote(pe=None, pb=None, market_cap=None, timestamp="2026-05-13 15:00:00")
        klines = [_kline(date="2026-05-13", close=100.0, volume=2000.0) for _ in range(80)]
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
        )

        report = build_financial_health(analysis)
        metrics = {item.name: item for item in report.metrics}

        self.assertIn("PE", report.missing_data)
        self.assertIn("PB", report.missing_data)
        self.assertIn("总市值", report.missing_data)
        self.assertEqual(metrics["市盈率"].value, "待接入")
        self.assertEqual(metrics["市净率"].value, "待接入")
        self.assertTrue(report.risk_notes)

    def test_profile_industry_and_quote_fields_build_financial_metric_cards(self) -> None:
        quote = _quote(pe=18.5, pb=2.4, market_cap=2_500_000_000, turnover_rate=2.2, timestamp="2026-05-13 15:00:00")
        klines = [_kline(date="2026-05-13", close=100.0, volume=2000.0) for _ in range(80)]
        analysis = build_analysis(
            quote,
            klines,
            stock_profile=_stock_info(),
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
        )

        report = build_financial_health(analysis)
        metric_names = [item.name for item in report.metrics]

        self.assertEqual(metric_names, ["市盈率", "市净率", "总市值", "所属行业", "交易活跃度"])
        self.assertNotIn("PE", report.missing_data)
        self.assertNotIn("PB", report.missing_data)
        self.assertTrue(report.highlights)

    def test_liquidity_metric_value_includes_turnover_when_available(self) -> None:
        self.assertEqual(liquidity_metric_value(123_000_000, None), "成交额 1.2亿")
        self.assertEqual(liquidity_metric_value(123_000_000, 2.345), "成交额 1.2亿 / 换手 2.35%")


if __name__ == "__main__":
    unittest.main()
