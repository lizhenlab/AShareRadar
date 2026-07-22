from __future__ import annotations

import unittest
from datetime import datetime

from app.models.schemas import AbnormalEventItem, AbnormalEventSummary
from app.services.analysis import build_analysis
from app.services.stock_event_sources import EXTERNAL_EVENT_RULES, external_event_placeholders
from app.services.data_quality import build_data_quality
from app.services.stock_event_summary import build_event_summary
from app.services.stock_lhb import build_lhb_summary
from tests.factories import make_kline as _kline
from tests.factories import make_plate_item as _plate_item
from tests.factories import make_quote as _quote


class StockEventSummaryTests(unittest.TestCase):
    def test_quiet_analysis_keeps_default_observation_event(self) -> None:
        quote = _quote(price=100.2, prev_close=100.0, high=101.0, low=99.5, change_pct=0.2, timestamp="2026-05-13 15:00:00")
        klines = [_kline(close=100.0, volume=1000.0, date="2026-05-13") for _ in range(80)]
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
        )

        summary = build_event_summary(analysis)

        self.assertEqual(len(summary.events), 1)
        self.assertEqual(summary.events[0].title, "暂无高强度事件")
        self.assertEqual(summary.events[0].category, "观察")
        self.assertIn("交易所公告", summary.missing_sources)

    def test_unavailable_external_sources_only_create_verification_steps(self) -> None:
        quote = _quote(price=109.0, prev_close=100.0, high=110.0, low=99.0, change_pct=9.0, turnover_rate=13.0, timestamp="2026-05-13 15:00:00")
        klines = [_kline(close=100.0 + index * 0.3, volume=1200.0 + index * 50, date="2026-05-13") for index in range(80)]
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
        )
        lhb = build_lhb_summary(analysis)

        summary = build_event_summary(analysis, lhb=lhb)
        categories = [item.category for item in summary.events]

        self.assertTrue({"龙虎榜", "公告", "融资融券"}.isdisjoint(categories))
        self.assertTrue(all(item.status == "unavailable" for item in summary.source_capabilities))
        self.assertTrue(any("异动核查建议" in step and "正式龙虎榜" in step for step in summary.next_steps))
        self.assertTrue(any("异动核查建议" in step and "融资融券" in step for step in summary.next_steps))
        self.assertEqual(external_event_placeholders(analysis, lhb), [])

    def test_external_event_rule_order_is_explicit(self) -> None:
        self.assertEqual(
            [rule.name for rule in EXTERNAL_EVENT_RULES],
            ["lhb_verification", "announcement_verification", "margin_financing_verification"],
        )

    def test_high_risk_adds_announcement_check_without_fabricating_event(self) -> None:
        quote = _quote(price=101.0, prev_close=100.0, high=102.0, low=99.0, change_pct=1.0, turnover_rate=2.0, timestamp="2026-05-13 15:00:00")
        klines = [_kline(close=100.0 + index * 0.1, volume=1000.0, date="2026-05-13") for index in range(80)]
        analysis = build_analysis(
            quote,
            klines,
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
        ).model_copy(update={"risk_level": "高风险"})

        summary = build_event_summary(analysis)

        self.assertNotIn("公告", [item.category for item in summary.events])
        self.assertTrue(any("异动核查建议" in step and "交易所公告" in step for step in summary.next_steps))

    def test_event_sources_preserve_review_industry_and_abnormal_order(self) -> None:
        quote = _quote(price=103.0, prev_close=100.0, high=104.0, low=99.0, change_pct=3.0, timestamp="2026-05-13 15:00:00")
        klines = [_kline(close=100.0 + index * 0.2, volume=1000.0 + index * 20, date="2026-05-13") for index in range(80)]
        analysis = build_analysis(
            quote,
            klines,
            industry_context=_plate_item(change_pct=1.8),
            data_quality=build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)),
        )
        abnormal = AbnormalEventSummary(
            symbol="600519.SH",
            updated_at="2026-05-13 10:00:00",
            score=63,
            level="偏强",
            main_signal="放量上涨",
            events=[
                AbnormalEventItem(
                    date="2026-05-13 10:00:00",
                    title="放量上涨",
                    level="积极",
                    direction="向上",
                    description="成交放大且价格上涨。",
                    watch_points=["确认次日承接。"],
                )
            ],
        )

        summary = build_event_summary(analysis, abnormal_events=abnormal)

        self.assertEqual([item.category for item in summary.events[:2]], ["行业", "异动"])
        self.assertEqual(summary.events[1].action_hint, "确认次日承接。")


if __name__ == "__main__":
    unittest.main()
