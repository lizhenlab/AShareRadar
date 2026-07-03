from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.models.schemas import Quote
from app.services.analysis import build_analysis
from app.services.stock_abnormal_context import build_abnormal_context
from app.services.stock_abnormal_events import build_abnormal_events
from tests.factories import make_kline as _kline


class StockAbnormalEventTests(unittest.TestCase):
    def test_quiet_quote_returns_neutral_summary(self) -> None:
        analysis = build_analysis(
            _quote(price=100.5, prev_close=100.0, open_price=100.0, high=101.0, low=99.8, change_pct=0.5),
            [_kline(close=100.0, volume=1000.0) for _ in range(25)],
        )

        summary = build_abnormal_events(analysis)

        self.assertEqual(summary.score, 50)
        self.assertEqual(summary.level, "中性")
        self.assertEqual(summary.main_signal, "暂无明显异动")
        self.assertEqual(summary.events, [])

    def test_risk_signal_wins_when_positive_and_risk_events_are_mixed(self) -> None:
        klines = [_kline(close=100.0, volume=1000.0) for _ in range(24)]
        klines.append(_kline(close=110.0, high=111.0, low=97.0, volume=3000.0))
        analysis = build_analysis(
            _quote(
                price=110.0,
                prev_close=100.0,
                open_price=98.0,
                high=110.0,
                low=97.0,
                change_pct=10.0,
                volume=3000.0,
            ),
            klines,
        )

        summary = build_abnormal_events(analysis)
        titles = [item.title for item in summary.events]

        self.assertIn("放量上涨", titles)
        self.assertIn("向下跳空", titles)
        self.assertIn("接近涨停", titles)
        self.assertEqual(summary.main_signal, "向下跳空")
        self.assertEqual(summary.level, "风险")

    def test_volume_down_and_limit_down_are_reported_as_risk(self) -> None:
        klines = [_kline(close=100.0, volume=1000.0) for _ in range(24)]
        klines.append(_kline(close=90.0, high=99.0, low=90.0, volume=2800.0))
        analysis = build_analysis(
            _quote(price=90.0, prev_close=100.0, open_price=99.0, high=99.0, low=90.0, change_pct=-10.0, volume=2800.0),
            klines,
        )

        summary = build_abnormal_events(analysis)
        titles = [item.title for item in summary.events]

        self.assertIn("放量下跌", titles)
        self.assertIn("接近跌停", titles)
        self.assertEqual(summary.level, "风险")
        self.assertEqual(summary.main_signal, "放量下跌")

    def test_abnormal_context_prefers_quote_prev_close_and_calculates_metrics(self) -> None:
        rows = [_kline(close=80.0, volume=1000.0) for _ in range(5)]
        rows.append(_kline(close=110.0, high=112.0, low=95.0, volume=3000.0))
        quote = _quote(
            price=110.0,
            prev_close=100.0,
            open_price=98.0,
            high=112.0,
            low=95.0,
            change_pct=10.0,
            volume=3000.0,
        )

        context = build_abnormal_context(SimpleNamespace(quote=quote, klines=rows))

        self.assertEqual(context.prev_close, 100.0)
        self.assertEqual(context.avg_volume, 1000.0)
        self.assertEqual(context.latest_volume, 3000.0)
        self.assertEqual(context.volume_ratio, 3.0)
        self.assertEqual(context.amplitude_pct, 17.0)
        self.assertEqual(context.upper_shadow_pct, 2.0)
        self.assertEqual(context.lower_shadow_pct, 3.0)

    def test_abnormal_context_falls_back_safely_when_base_or_volume_is_missing(self) -> None:
        rows = [_kline(close=90.0, volume=0.0)]
        quote = _quote(price=0.0, prev_close=0.0, open_price=0.0, high=10.0, low=0.0, change_pct=0.0, volume=0.0)

        context = build_abnormal_context(SimpleNamespace(quote=quote, klines=rows))

        self.assertEqual(context.prev_close, 0.0)
        self.assertEqual(context.latest_volume, 0.0)
        self.assertIsNone(context.volume_ratio)
        self.assertEqual(context.amplitude_pct, 0)
        self.assertEqual(context.upper_shadow_pct, 0)
        self.assertEqual(context.lower_shadow_pct, 0)

    def test_abnormal_events_use_today_quote_volume_when_klines_stop_yesterday(self) -> None:
        klines = [_kline(close=100.0, volume=1000.0, date=f"2026-05-{day:02d}") for day in range(1, 26)]
        quote = _quote(
            price=102.0,
            prev_close=100.0,
            open_price=100.0,
            high=103.0,
            low=99.0,
            change_pct=2.0,
            volume=2500.0,
            timestamp="2026-05-26 10:00:00",
        )

        summary = build_abnormal_events(build_analysis(quote, klines))

        self.assertIn("放量上涨", [item.title for item in summary.events])

    def test_abnormal_context_falls_back_to_kline_volume_when_quote_volume_is_invalid(self) -> None:
        rows = [_kline(close=100.0, volume=1000.0, date=f"2026-05-{day:02d}") for day in range(8, 13)]
        rows.append(_kline(close=103.0, volume=3000.0, date="2026-05-13"))
        quote = _quote(
            price=103.0,
            prev_close=100.0,
            open_price=100.0,
            high=104.0,
            low=99.0,
            change_pct=3.0,
            volume=0.0,
            timestamp="2026-05-13 10:00:00",
        )

        context = build_abnormal_context(SimpleNamespace(quote=quote, klines=rows))

        self.assertEqual(context.avg_volume, 1000.0)
        self.assertEqual(context.latest_volume, 3000.0)
        self.assertEqual(context.volume_ratio, 3.0)


def _quote(
    *,
    price: float,
    prev_close: float,
    open_price: float,
    high: float,
    low: float,
    change_pct: float,
    volume: float = 1000.0,
    timestamp: str = "2026-05-13 10:00:00",
) -> Quote:
    return Quote(
        code="600519",
        name="贵州茅台",
        market="SH",
        price=price,
        prev_close=prev_close,
        open=open_price,
        high=high,
        low=low,
        volume=volume,
        amount=100_000_000,
        change=round(price - prev_close, 2),
        change_pct=change_pct,
        timestamp=timestamp,
        source="测试行情",
    )


if __name__ == "__main__":
    unittest.main()
