from __future__ import annotations

import unittest
from datetime import datetime

from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.financial_health import build_financial_health
from app.services.financial_health_components import FORMAL_FINANCIAL_FIELDS, liquidity_metric_value
from app.services.research_features import _financial_health_score
from app.models.schemas import FinancialHealth
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
        self.assertIsNone(report.score)
        self.assertFalse(report.score_available)
        self.assertFalse(report.formal_minimum_complete)
        self.assertEqual(report.metric_scope, "market_valuation_trading_vitals")
        self.assertIn("暂不生成财务体检分", report.summary)
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
        self.assertIsNone(report.score)
        self.assertFalse(report.score_available)
        self.assertEqual(_financial_health_score(report), None)
        self.assertEqual(
            [item.category for item in report.metrics],
            ["market_valuation", "market_valuation", "market_valuation", "context", "trading_vital"],
        )
        self.assertNotIn("基础财务体检", report.summary)
        self.assertTrue(report.highlights)

    def test_formal_financial_minimum_includes_period_and_core_statement_fields(self) -> None:
        self.assertEqual(FORMAL_FINANCIAL_FIELDS, ["报告期", "ROE", "营收增速", "净利润增速", "经营现金流", "资产负债率"])

    def test_legacy_market_field_score_parses_but_is_not_reexposed_as_financial_health(self) -> None:
        report = FinancialHealth(
            symbol="000001.SZ",
            updated_at="2026-05-13 15:00:00",
            score=88,
            level="强",
            summary="旧行情字段体检",
            metrics=[],
            source="旧数据",
        )

        self.assertIsNone(report.score)
        self.assertFalse(report.score_available)
        self.assertEqual(report.level, "不可用")
        self.assertEqual(report.metric_scope, "legacy_unspecified")

    def test_non_finite_market_fields_remain_unavailable_without_a_health_score(self) -> None:
        quote = _quote(timestamp="2026-05-13 15:00:00").model_copy(
            update={"pe": float("nan"), "pb": float("inf"), "market_cap": float("-inf"), "amount": float("nan")}
        )
        klines = [_kline(date="2026-05-13", close=100.0, volume=2000.0) for _ in range(80)]
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
        )

        report = build_financial_health(analysis)

        self.assertIsNone(report.score)
        self.assertTrue({"PE", "PB", "总市值", "成交额"}.issubset(report.missing_data))
        self.assertTrue(all(item.level == "不可用" for item in report.metrics if item.name != "所属行业"))

    def test_liquidity_metric_value_includes_turnover_when_available(self) -> None:
        self.assertEqual(liquidity_metric_value(123_000_000, None), "成交额 1.2亿")
        self.assertEqual(liquidity_metric_value(123_000_000, 2.345), "成交额 1.2亿 / 换手 2.35%")


if __name__ == "__main__":
    unittest.main()
