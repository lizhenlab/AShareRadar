from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import math
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_datahub
from app.api.routes import stock as stock_routes
from app.models.schemas import AlertRuleInput, MinuteKline, Quote, StockConceptItem
from app.services.cache import SQLiteCache
from app.services.alerts import evaluate_alert_rules
from app.services.analysis import build_analysis
from app.services.data_quality import assess_kline_quality, build_data_quality
from app.services.datahub import DataHub
from app.services.indicators import max_drawdown, recent_volume_ratio, support_resistance, trend_score, trend_score_snapshot
from app.services.market_sampling import fetch_quotes_with_single_fallback, market_breadth_quotes, market_breadth_symbols
from app.services.minute_analysis import build_minute_analysis_report
from app.services.research import (
    answer_stock_question,
    build_alpha_evidence_report,
    build_chip_analysis,
    build_event_digest_report,
    build_evidence_chain_report,
    build_factor_lab_report,
    build_feature_snapshot,
    build_leadership_report,
    build_market_breadth_snapshot,
    build_market_regime_report,
    build_peer_comparison_report,
    build_replay_analysis,
    build_risk_reward_report,
    build_risk_radar_report,
    build_signal_validation_report,
    build_stock_diagnosis,
    build_stock_qa_report,
    build_theme_context_report,
    build_t_strategy_assistant_report,
    build_timeframe_alignment_report,
)
from app.services.stock_finance import _valuation_percentile_from_history
from app.services.stock_insights import build_stock_insight_bundle
from app.config import Settings
from app.utils.errors import NotFoundError
from app.workflows.individual import stock_minute_analysis
from app.workflows.market_overview import strong_stock_watch as workflow_strong_stock_watch
from tests.factories import (
    make_kline as _kline,
    make_quote as _quote,
    make_stock_info as _stock_info,
)


def _local_only_datahub(path: Path) -> DataHub:
    settings = Settings(cache_path=path, stock_provider_priority=("local",))
    return DataHub(cache=SQLiteCache(settings=settings), settings=settings)


class MinuteAnalysisTests(unittest.TestCase):
    def test_minute_analysis_builds_t_plan_from_intraday_levels(self) -> None:
        rows = [
            MinuteKline(
                timestamp=f"2026-05-15 10:{index:02d}:00",
                open=100 + index * 0.05,
                close=100 + index * 0.06,
                high=100.3 + index * 0.06,
                low=99.8 + index * 0.04,
                volume=1000 + index * 80,
                amount=10_000_000 + index * 100_000,
                interval="5m",
                source="测试分钟线",
            )
            for index in range(30)
        ]
        report = build_minute_analysis_report("600519.SH", rows, interval="5m")

        self.assertEqual(report.interval, "5m")
        self.assertEqual(report.sample_count, len(report.klines))
        self.assertGreaterEqual(report.sample_count, 30)
        self.assertTrue(report.supports)
        self.assertTrue(report.resistances)
        self.assertIn(report.t_plan.suitability, {"仅底仓可做T", "等待更大区间", "不适合主动做T"})
        self.assertTrue(report.t_plan.execution_steps)
        self.assertTrue(report.t_plan.stop_conditions)

    def test_minute_analysis_returns_safe_report_when_source_fails(self) -> None:
        async def run_check(path: Path):
            hub = _local_only_datahub(path)
            with patch.object(hub, "minute_kline", side_effect=RuntimeError("所有分钟K线数据源均不可用：akshare: ProxyError('Unable to connect to proxy')")):
                return await stock_minute_analysis(hub, "600900", interval="5m", limit=120)

        with TemporaryDirectory() as tmpdir:
            report = __import__("asyncio").run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(report.symbol, "600900.SH")
        self.assertEqual(report.availability, "unavailable")
        self.assertEqual(report.reason_code, "provider_failure")
        self.assertEqual(report.klines, [])
        self.assertEqual(report.t_plan.suitability, "暂停做T判断")
        self.assertEqual(report.t_plan.low_zone, "不可用")
        self.assertEqual(report.t_plan.high_zone, "不可用")
        self.assertIn("分钟K线", report.missing_data)
        self.assertIn("网络代理连接失败", report.summary)

    def test_minute_analysis_source_failure_ignores_local_event_log_failure(self) -> None:
        class FailingEventCache:
            def log_event(self, category: str, message: str) -> None:
                raise RuntimeError("event db readonly")

        class FakeHub:
            def __init__(self) -> None:
                self.cache = FailingEventCache()

            async def stock_profile(self, symbol: str):
                return _stock_info(code="600519", market="SH")

            async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120):
                raise ConnectionError("HTTPSConnectionPool: remote end closed connection")

        report = asyncio.run(stock_minute_analysis(FakeHub(), "600519", interval="5m", limit=120))  # type: ignore[arg-type]

        self.assertEqual(report.symbol, "600519.SH")
        self.assertEqual(report.availability, "unavailable")
        self.assertEqual(report.reason_code, "provider_failure")
        self.assertEqual(report.klines, [])
        self.assertEqual(report.t_plan.suitability, "暂停做T判断")
        self.assertIn("行情接口连接失败", report.summary)

    def test_stock_minute_analysis_normalizes_interval_alias_before_fetch_and_report(self) -> None:
        async def run_check(path: Path):
            hub = _local_only_datahub(path)
            calls = []

            async def minute_kline(symbol: str, interval: str = "5m", limit: int = 120):
                calls.append((symbol, interval, limit))
                return [
                    MinuteKline(
                        timestamp=f"2026-05-15 10:{index:02d}:00",
                        open=100,
                        close=100 + index * 0.01,
                        high=100.5 + index * 0.01,
                        low=99.5,
                        volume=1000 + index,
                        interval=interval,
                    )
                    for index in range(12)
                ]

            with patch.object(hub, "minute_kline", side_effect=minute_kline):
                report = await stock_minute_analysis(hub, "600519", interval="5min", limit=12)
            return report.interval, calls

        with TemporaryDirectory() as tmpdir:
            interval, calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(interval, "5m")
        self.assertEqual(calls, [("600519.SH", "5m", 12)])

    def test_minute_analysis_route_enforces_documented_interval_whitelist(self) -> None:
        class FakeHub:
            def __init__(self) -> None:
                self.minute_calls: list[str] = []

            async def stock_profile(self, symbol: str):
                return _stock_info(code="600519", market="SH")

            async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120):
                self.minute_calls.append(interval)
                return [
                    MinuteKline(
                        timestamp=f"2026-05-15 10:{index:02d}:00",
                        open=100,
                        close=100,
                        high=101,
                        low=99,
                        volume=1000 + index,
                        interval=interval,
                        source="测试分钟线",
                    )
                    for index in range(8)
                ]

        hub = FakeHub()
        app = FastAPI()
        app.include_router(stock_routes.router)
        app.dependency_overrides[get_datahub] = lambda: hub
        client = TestClient(app)
        supported = ["1m", "5m", "15m", "30m", "60m"]

        for interval in supported:
            response = client.get("/api/stock/minute-analysis", params={"symbol": "600519", "interval": interval, "limit": 20})
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["sample_count"], len(payload["klines"]))
            self.assertEqual(len(payload["klines"]), 8)
            self.assertEqual(payload["klines"][0]["interval"], interval)
            self.assertEqual(payload["klines"][0]["source"], "测试分钟线")
            self.assertFalse(payload["klines"][0]["from_cache"])
            self.assertFalse(payload["klines"][0]["fallback_used"])

        accepted_call_count = len(hub.minute_calls)
        for interval in ["3m", "10m", "5min", "2m", ""]:
            response = client.get("/api/stock/minute-analysis", params={"symbol": "600519", "interval": interval, "limit": 20})
            self.assertEqual(response.status_code, 422, response.text)

        self.assertEqual(hub.minute_calls, supported)
        self.assertEqual(len(hub.minute_calls), accepted_call_count)
        interval_parameter = next(
            item
            for item in app.openapi()["paths"]["/api/stock/minute-analysis"]["get"]["parameters"]
            if item["name"] == "interval"
        )
        self.assertEqual(interval_parameter["schema"]["enum"], supported)
        self.assertEqual(interval_parameter["description"], "分钟周期：1m/5m/15m/30m/60m")
        limit_parameter = next(
            item
            for item in app.openapi()["paths"]["/api/stock/minute-analysis"]["get"]["parameters"]
            if item["name"] == "limit"
        )
        self.assertEqual(limit_parameter["schema"]["maximum"], 500)

    def test_stock_minute_analysis_confirms_symbol_before_fetching_minute_rows(self) -> None:
        async def run_check(path: Path):
            hub = _local_only_datahub(path)
            fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            hub.cache.save_stock_pool(
                [
                    _stock_info(code=f"60{index:04d}", market="SH").model_copy(update={"updated_at": fresh_time})
                    for index in range(1000)
                ]
            )
            calls = []

            async def minute_kline(symbol: str, interval: str = "5m", limit: int = 120):
                calls.append((symbol, interval, limit))
                return []

            with patch.object(hub, "quote", side_effect=RuntimeError("quote unavailable")):
                with patch.object(hub, "minute_kline", side_effect=minute_kline):
                    with self.assertRaises(NotFoundError):
                        await stock_minute_analysis(hub, "688001", interval="5m", limit=120)
            return calls

        with TemporaryDirectory() as tmpdir:
            calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(calls, [])

    def test_demo_quote_gets_low_quality_score(self) -> None:
        quality = build_data_quality(_quote(source="本地演示数据"), [])

        self.assertLess(quality.score, 50)
        self.assertIn("演示行情", quality.anomalies)

    def test_support_resistance_ignores_single_wick_when_not_broken(self) -> None:
        klines = [
            _kline(close=100 + index, high=101 + index, low=99 + index, volume=1000 + index * 10)
            for index in range(24)
        ]
        klines[-1] = _kline(close=120, high=121, low=118, volume=2000)
        support, resistance = support_resistance(klines)

        self.assertGreater(support, 99)
        self.assertGreater(resistance, 110)

    def test_support_resistance_uses_realtime_price_for_intraday_breakout(self) -> None:
        klines = [_kline(close=100, high=103, low=98, volume=1000) for _ in range(24)]
        klines[-1] = _kline(close=100, high=115, low=99, volume=1800)
        last_close_resistance = support_resistance(klines)[1]
        realtime_resistance = support_resistance(klines, current_price=120)[1]

        self.assertGreater(realtime_resistance, last_close_resistance)

    def test_support_resistance_ignores_invalid_recent_low_when_breaking_down(self) -> None:
        klines = [_kline(close=100, high=103, low=98, volume=1000) for _ in range(24)]
        klines[-1] = _kline(close=90, high=91, low=0, volume=1200)
        support, _ = support_resistance(klines, current_price=89)

        self.assertGreater(support, 0)

    def test_trend_score_rewards_price_volume_confirmation(self) -> None:
        base = [_kline(close=100 + index * 0.4, high=101 + index * 0.4, low=99 + index * 0.4, volume=1000) for index in range(30)]
        normal_quote = _quote(price=115.0, prev_close=113.0, high=116.0, low=112.0, change_pct=1.7, turnover_rate=4.0)
        strong_quote = _quote(price=115.0, prev_close=113.0, high=116.0, low=112.0, change_pct=3.8, turnover_rate=4.0)
        strong_quote.amount = 2_000_000_000
        base[-5:] = [_kline(close=111 + index, high=112 + index, low=110 + index, volume=2500) for index in range(5)]

        normal_score, _ = trend_score(normal_quote, base)
        strong_score, _ = trend_score(strong_quote, base)

        self.assertGreaterEqual(strong_score, normal_score)

    def test_trend_score_exposes_contribution_breakdown(self) -> None:
        klines = [_kline(close=100 + index * 0.5, high=101 + index * 0.5, low=99 + index * 0.5, volume=1000) for index in range(30)]
        quote = _quote(price=116, prev_close=113, high=117, low=112, change_pct=2.65, turnover_rate=4.0)

        score, label, contributions = trend_score_snapshot(quote, klines)
        plain_score, plain_label = trend_score(quote, klines)
        reconstructed = max(0, min(100, 50 + sum(item.impact for item in contributions)))

        self.assertEqual(score, plain_score)
        self.assertEqual(label, plain_label)
        self.assertEqual(score, reconstructed)
        self.assertTrue(any(item.impact > 0 for item in contributions))

    def test_build_analysis_filters_invalid_klines_from_result_but_scores_raw_quality(self) -> None:
        klines = [_kline(close=100 + index, high=101 + index, low=99 + index, volume=1000) for index in range(30)]
        klines.append(_kline(close=140, high=141, low=139, volume=1000).model_copy(update={"high": math.inf}))
        quote = _quote(price=130, prev_close=128, high=131, low=127, change_pct=1.56)

        analysis = build_analysis(quote, klines)

        self.assertEqual(len(analysis.klines), 30)
        self.assertEqual(analysis.data_quality.kline_count, 31)
        self.assertTrue(all(math.isfinite(item.high) for item in analysis.klines))

    def test_build_analysis_copies_optional_history_and_peer_inputs(self) -> None:
        quote = _quote(price=130, prev_close=128, high=131, low=127, change_pct=1.56)
        klines = [_kline(close=100 + index, high=101 + index, low=99 + index, volume=1000) for index in range(30)]
        quote_history = [{"price": 120.0, "quote_timestamp": "2026-05-13 10:00:00"}]
        peer_quotes = [_quote(price=128, prev_close=127, high=129, low=126)]

        analysis = build_analysis(quote, klines, quote_history=quote_history, peer_quotes=peer_quotes)
        quote_history.append({"price": 999.0, "quote_timestamp": "2026-05-14 10:00:00"})
        peer_quotes.clear()

        self.assertEqual(len(analysis.quote_history), 1)
        self.assertEqual(analysis.quote_history[0]["price"], 120.0)
        self.assertEqual(len(analysis.peer_quotes), 1)
        self.assertEqual(analysis.peer_sample.status, "available")
        self.assertEqual(analysis.peer_sample.requested_count, 1)

    def test_recent_volume_ratio_is_stable(self) -> None:
        klines = [_kline(volume=1000 + index * 20) for index in range(30)]
        self.assertGreater(recent_volume_ratio(klines), 1.0)

    def test_max_drawdown_accepts_empty_series(self) -> None:
        self.assertEqual(max_drawdown([]), 0)

    def test_stale_kline_quality_penalizes_analysis_data(self) -> None:
        klines = [_kline(date="2000-01-03", source="腾讯行情", from_cache=True, fallback_used=True) for _ in range(80)]
        quality = build_data_quality(_quote(), klines)

        self.assertLess(quality.score, 70)
        self.assertIsNotNone(quality.kline_quality)
        assert quality.kline_quality is not None
        self.assertGreaterEqual(quality.kline_quality.days_behind_expected or 0, 1)
        self.assertIn("K线兜底缓存", quality.anomalies)

    def test_assess_kline_quality_accepts_current_kline(self) -> None:
        klines = [_kline(date="2026-05-13", source="腾讯行情") for _ in range(80)]
        quality = assess_kline_quality(klines, now=__import__("datetime").datetime(2026, 5, 13, 16, 0, 0))

        self.assertEqual(quality.level, "良好")
        self.assertEqual(quality.days_behind_expected, 0)

    def test_data_quality_checked_at_uses_injected_now(self) -> None:
        now = datetime(2026, 5, 13, 16, 0, 0)
        klines = [_kline(date="2026-05-13", source="腾讯行情") for _ in range(80)]
        quality = build_data_quality(_quote(timestamp="2026-05-13 15:00:00"), klines, now=now)

        self.assertEqual(quality.checked_at, "2026-05-13 16:00:00")

    def test_after_hours_same_day_quote_is_not_severely_penalized(self) -> None:
        now = datetime(2026, 5, 13, 21, 31, 0)
        klines = [_kline(date="2026-05-13", source="腾讯行情", from_cache=True) for _ in range(80)]
        quality = build_data_quality(_quote(source="腾讯行情·缓存", timestamp="2026-05-13 16:14:27"), klines, now=now)

        self.assertGreaterEqual(quality.score, 85)
        self.assertNotIn("报价严重滞后", quality.anomalies)
        self.assertIn("盘后使用当天行情快照", "；".join(quality.notes))

    def test_intraday_old_quote_is_penalized_during_session(self) -> None:
        now = datetime(2026, 5, 13, 10, 30, 0)
        klines = [_kline(date="2026-05-13", source="腾讯行情") for _ in range(80)]
        quality = build_data_quality(_quote(source="腾讯行情·缓存", timestamp="2026-05-12 16:14:27"), klines, now=now)

        self.assertLess(quality.score, 80)
        self.assertIn("报价滞后", quality.anomalies)
        self.assertIn("交易时段仍在使用", "；".join(quality.notes))

    def test_low_quality_analysis_downgrades_buy_and_t_plan(self) -> None:
        klines = [
            _kline(date="2000-01-03", close=100 + index, high=101 + index, low=99 + index, volume=2000, from_cache=True, fallback_used=True)
            for index in range(80)
        ]
        quote = _quote(price=181, prev_close=176, high=183, low=175, change_pct=2.84, turnover_rate=4.0)
        quality = build_data_quality(quote, klines)
        analysis = build_analysis(quote, klines, data_quality=quality)

        self.assertEqual(analysis.action_advice.action, "控制风险")
        self.assertEqual(analysis.buy_points[0].title, "暂停新增买点")
        self.assertEqual(analysis.t_plan[0].title, "暂停做T")
        self.assertTrue(analysis.signal_snapshot.risk_notes)

    def test_t_plan_has_style_and_invalidation(self) -> None:
        start_date = date(2026, 4, 14)
        klines = [
            _kline(
                date=(start_date + timedelta(days=index)).isoformat(),
                close=100 + index,
                high=101 + index,
                low=99 + index,
                volume=2000,
            )
            for index in range(30)
        ]
        quote = _quote(price=129, prev_close=125, high=130, low=125, change_pct=3.2, turnover_rate=4.0)
        quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
        analysis = build_analysis(quote, klines, data_quality=quality)

        titles = [item.title for item in analysis.t_plan]
        self.assertTrue(any("高抛区" in item for item in titles))
        self.assertTrue(any("低吸区" in item for item in titles))
        self.assertIn("做T失效条件", titles)

    def test_low_quality_strategies_and_rules_are_downshifted(self) -> None:
        klines = [
            _kline(date="2000-01-03", close=100 + index * 1.2, high=101 + index * 1.2, low=99 + index * 1.2, volume=1000 if index < 24 else 5000, from_cache=True, fallback_used=True)
            for index in range(30)
        ]
        quote = _quote(price=138, prev_close=130, high=139, low=129, change_pct=6.15, turnover_rate=5.0)
        quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 10, 30, 0))
        analysis = build_analysis(quote, klines, data_quality=quality)
        bundle = build_stock_insight_bundle(analysis)

        self.assertLess(analysis.data_quality.score, 70)
        self.assertIn(bundle.strategy_cards[0].status, {"等待确认", "暂停观察", "暂停"})
        breakout = next(item for item in bundle.rule_matches.matches if item.rule_id == "volume_breakout_20d")
        self.assertNotEqual(breakout.status, "命中")
        self.assertLess(breakout.confidence, 78)

    def test_strategy_cards_tolerate_empty_signal_lists(self) -> None:
        klines = [
            _kline(date=f"2026-05-{index + 1:02d}", close=100 + index, high=101 + index, low=99 + index, volume=2000)
            for index in range(30)
        ]
        quote = _quote(price=129, prev_close=127, high=130, low=126, change_pct=1.57, turnover_rate=4.0)
        quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
        analysis = build_analysis(quote, klines, data_quality=quality).model_copy(update={"buy_points": [], "sell_points": [], "t_plan": []})
        bundle = build_stock_insight_bundle(analysis)

        self.assertEqual(len(bundle.strategy_cards), 5)
        self.assertEqual(bundle.strategy_cards[0].trigger_conditions, ["暂无清晰买点"])
        self.assertEqual(bundle.strategy_cards[3].trigger_conditions, ["暂无做T区间"])
        self.assertEqual(bundle.strategy_cards[4].trigger_conditions, ["暂无清晰卖点"])

    def test_research_outputs_share_feature_snapshot_and_quality_gate(self) -> None:
        klines = [
            _kline(date=f"2026-04-{index + 1:02d}", close=100 + index * 0.8, high=101 + index * 0.8, low=99 + index * 0.8, volume=1200 + index * 30)
            for index in range(28)
        ]
        klines.extend(
            _kline(date=f"2026-05-{index + 1:02d}", close=123 + index * 1.1, high=125 + index * 1.1, low=121 + index * 1.1, volume=2600 + index * 120)
            for index in range(12)
        )
        quote = _quote(price=137.5, prev_close=133.0, high=139.0, low=132.5, change_pct=3.38, turnover_rate=4.6)
        quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
        analysis = build_analysis(quote, klines, data_quality=quality)
        bundle = build_stock_insight_bundle(analysis)
        feature = build_feature_snapshot(analysis, bundle)
        chip = build_chip_analysis(analysis, feature)
        leadership = build_leadership_report(analysis, bundle, feature)
        factor_lab = build_factor_lab_report(analysis, bundle, feature, chip, leadership)
        regime = build_market_regime_report(analysis, bundle, feature, factor_lab)
        timeframe = build_timeframe_alignment_report(analysis, feature, factor_lab)
        validation = build_signal_validation_report(analysis, feature, factor_lab, regime, timeframe)
        risk_reward = build_risk_reward_report(analysis, feature, factor_lab, regime, validation, timeframe)
        alpha = build_alpha_evidence_report(analysis, bundle, feature, factor_lab, regime, timeframe, risk_reward)
        diagnosis = build_stock_diagnosis(analysis, bundle, feature, alpha, factor_lab, regime, validation, risk_reward, timeframe)
        evidence_chain = build_evidence_chain_report(diagnosis, alpha, validation, risk_reward)
        t_strategy = build_t_strategy_assistant_report(analysis, feature, regime, validation)
        theme_context = build_theme_context_report(
            analysis,
            feature,
            [
                StockConceptItem(
                    symbol="600519.SH",
                    rank=1,
                    name="白酒概念",
                    change_pct=1.2,
                    leading_stock="贵州茅台",
                    source="测试概念源",
                    updated_at="2026-05-15 10:00:00",
                )
            ],
        )
        qa_report = build_stock_qa_report(analysis, diagnosis, regime, risk_reward, t_strategy, theme_context)
        event_digest = build_event_digest_report(bundle)
        peer_comparison = build_peer_comparison_report(analysis, bundle, feature)
        risk_radar = build_risk_radar_report(analysis, bundle, feature, regime, risk_reward, timeframe)
        replay = build_replay_analysis(analysis)

        self.assertEqual(feature.symbol, "600519.SH")
        self.assertGreaterEqual(feature.leader_score, 0)
        self.assertGreaterEqual(len(factor_lab.factors), 6)
        self.assertGreaterEqual(factor_lab.total_score, 0)
        self.assertLessEqual(factor_lab.total_score, 100)
        self.assertGreaterEqual(factor_lab.calibrated_confidence, 0)
        self.assertLessEqual(factor_lab.calibrated_confidence, 100)
        self.assertGreaterEqual(factor_lab.calibration_sample_count, 0)
        self.assertGreaterEqual(factor_lab.positive_factor_count, 0)
        self.assertGreaterEqual(factor_lab.negative_factor_count, 0)
        self.assertIn(factor_lab.profile_label, {"常规个股", "大市值稳健股", "高活跃波动股", "低流动性个股"})
        self.assertTrue(factor_lab.weight_policy)
        self.assertTrue(factor_lab.summary)
        for factor in factor_lab.factors:
            self.assertGreaterEqual(factor.score, 0)
            self.assertLessEqual(factor.score, 100)
            self.assertGreaterEqual(factor.weight, 0.5)
            self.assertLessEqual(factor.weight, 1.8)
            self.assertIsNotNone(factor.calibration)
            assert factor.calibration is not None
            self.assertGreaterEqual(factor.calibration.stability_score, 0)
            self.assertLessEqual(factor.calibration.stability_score, 100)
            self.assertIn(factor.calibration.expected_level, {"较强", "偏正", "观察", "偏弱", "风险", "待确认", "待补数据", "样本不足"})
            for bucket in factor.calibration_buckets:
                self.assertGreater(bucket.sample_count, 0)
                self.assertIn(bucket.name, {"强趋势", "弱趋势", "支撑附近", "压力附近"})
        self.assertIn(regime.stock_state, {"数据不足", "风险优先", "右侧偏强", "支撑观察", "压力确认", "震荡等待"})
        self.assertGreater(regime.risk_multiplier, 0)
        self.assertTrue(regime.suggestions)
        self.assertEqual(validation.symbol, feature.symbol)
        self.assertGreaterEqual(len(validation.items), 4)
        self.assertIn(validation.overall_status, {"风险优先", "条件较好", "等待二次确认", "观察为主"})
        self.assertTrue(all(item.trigger_condition and item.confirmation_condition and item.invalidation_condition for item in validation.items))
        self.assertEqual(timeframe.symbol, feature.symbol)
        self.assertGreaterEqual(timeframe.alignment_score, 0)
        self.assertLessEqual(timeframe.alignment_score, 100)
        self.assertIn(timeframe.conflict_level, {"待确认", "高冲突", "中冲突", "多周期顺向", "多周期偏弱", "轻微分歧"})
        self.assertGreaterEqual(len(timeframe.timeframes), 1)
        self.assertTrue(timeframe.suggestions)
        self.assertEqual(risk_reward.symbol, feature.symbol)
        self.assertGreater(risk_reward.upside_target, 0)
        self.assertGreater(risk_reward.downside_stop, 0)
        self.assertGreaterEqual(risk_reward.reward_risk_ratio, 0)
        self.assertGreaterEqual(risk_reward.atr14, 0)
        self.assertGreaterEqual(risk_reward.atr_pct, 0)
        self.assertGreaterEqual(risk_reward.volatility_pct, 0)
        self.assertIn("ATR", risk_reward.summary)
        self.assertIn(risk_reward.rating, {"风险优先", "周期冲突", "等待确认", "性价比较好", "性价比一般", "性价比不足"})
        self.assertEqual(len(risk_reward.scenarios), 3)
        self.assertEqual(sum(item.probability for item in risk_reward.scenarios), 100)
        self.assertTrue(alpha.positives or alpha.negatives)
        self.assertIn(diagnosis.action, {"控制风险", "等待确认", "轻仓观察", "谨慎观察", "积极关注"})
        self.assertIn("因子实验室", diagnosis.professional_summary)
        self.assertTrue(evidence_chain.support or evidence_chain.opposition)
        self.assertGreaterEqual(len(qa_report.items), 4)
        self.assertTrue(any("概念题材" in item.question for item in qa_report.items))
        self.assertTrue(event_digest.summary)
        self.assertTrue(peer_comparison.summary)
        self.assertTrue(t_strategy.stop_conditions)
        self.assertGreaterEqual(len(risk_radar.items), 6)
        self.assertTrue(risk_radar.top_risks)
        t_answer = answer_stock_question(
            "适合做T吗",
            analysis,
            diagnosis,
            evidence_chain,
            risk_radar,
            event_digest,
            peer_comparison,
            t_strategy,
            regime,
            risk_reward,
            validation,
            timeframe,
        )
        theme_answer = answer_stock_question(
            "它有什么概念题材",
            analysis,
            diagnosis,
            evidence_chain,
            risk_radar,
            event_digest,
            peer_comparison,
            t_strategy,
            regime,
            risk_reward,
            validation,
            timeframe,
            theme_context,
        )
        theme_answer_without_context = answer_stock_question(
            "题材能支撑走势吗",
            analysis,
            diagnosis,
            evidence_chain,
            risk_radar,
            event_digest,
            peer_comparison,
            t_strategy,
            regime,
            risk_reward,
            validation,
            timeframe,
        )
        risk_reward_answer = answer_stock_question(
            "当前风险收益比够不够",
            analysis,
            diagnosis,
            evidence_chain,
            risk_radar,
            event_digest,
            peer_comparison,
            t_strategy,
            regime,
            risk_reward,
            validation,
            timeframe,
            theme_context,
        )
        dip_buy_answer = answer_stock_question(
            "能不能低吸买一点",
            analysis,
            diagnosis,
            evidence_chain,
            risk_radar,
            event_digest,
            peer_comparison,
            t_strategy,
            regime,
            risk_reward,
            validation,
            timeframe,
            theme_context,
        )
        risk_answer = answer_stock_question(
            "风险在哪里",
            analysis,
            diagnosis,
            evidence_chain,
            risk_radar,
            event_digest,
            peer_comparison,
            t_strategy,
            regime,
            risk_reward,
            validation,
            timeframe,
        )
        self.assertEqual(t_answer.topic, "做T")
        self.assertIn("做T", t_answer.answer)
        self.assertTrue(t_answer.evidence)
        self.assertEqual(theme_answer.topic, "主题概念")
        self.assertIn("白酒概念", "；".join(theme_answer.evidence))
        self.assertTrue(theme_answer.actions)
        self.assertTrue(any("题材" in item or "概念" in item for item in theme_answer.invalidations))
        self.assertLessEqual(theme_answer_without_context.confidence, theme_answer.confidence)
        self.assertIn("待确认", theme_answer_without_context.conclusion)
        self.assertEqual(risk_reward_answer.topic, "风险收益")
        self.assertIn("收益风险比", "；".join(risk_reward_answer.evidence))
        self.assertTrue(any("1.2" in item or "性价比" in item for item in risk_reward_answer.invalidations))
        self.assertEqual(dip_buy_answer.topic, "买点")
        self.assertIn("想买", dip_buy_answer.answer)
        self.assertEqual(risk_answer.topic, "风险")
        self.assertTrue(risk_answer.invalidations)
        self.assertGreater(chip.center_price, 0)
        self.assertEqual(leadership.score, feature.leader_score)
        self.assertGreaterEqual(replay.window_days, 30)

    def test_timeframe_conflict_downgrades_validation_and_diagnosis(self) -> None:
        klines = []
        base = 100.0
        for index in range(100):
            if index < 80:
                close = base + index * 0.6
            elif index < 90:
                close = 148 - (index - 80) * 1.1
            else:
                close = 137 + (index - 90) * 0.2
            klines.append(_kline(date=f"2026-05-{(index % 28) + 1:02d}", close=close, high=close + 1.2, low=close - 1.2, volume=3000 + index * 40))
        quote = _quote(price=139.2, prev_close=138.4, high=140.1, low=137.8, change_pct=0.58, turnover_rate=3.8)
        quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
        analysis = build_analysis(quote, klines, data_quality=quality)
        bundle = build_stock_insight_bundle(analysis)
        feature = build_feature_snapshot(analysis, bundle)
        chip = build_chip_analysis(analysis, feature)
        leadership = build_leadership_report(analysis, bundle, feature)
        factor_lab = build_factor_lab_report(analysis, bundle, feature, chip, leadership)
        regime = build_market_regime_report(analysis, bundle, feature, factor_lab)
        timeframe = build_timeframe_alignment_report(analysis, feature, factor_lab)
        validation = build_signal_validation_report(analysis, feature, factor_lab, regime, timeframe)
        risk_reward = build_risk_reward_report(analysis, feature, factor_lab, regime, validation, timeframe)
        alpha = build_alpha_evidence_report(analysis, bundle, feature, factor_lab, regime, timeframe, risk_reward)
        diagnosis = build_stock_diagnosis(analysis, bundle, feature, alpha, factor_lab, regime, validation, risk_reward, timeframe)

        self.assertIn(timeframe.conflict_level, {"高冲突", "中冲突", "多周期偏弱"})
        self.assertIn(risk_reward.rating, {"周期冲突", "风险优先"})
        self.assertIn(validation.overall_status, {"风险优先", "等待二次确认", "观察为主"})
        self.assertIn(diagnosis.action, {"控制风险", "谨慎观察"})

    def test_market_breadth_adjusts_regime_risk(self) -> None:
        klines = [
            _kline(date=f"2026-05-{index + 1:02d}", close=100 + index, high=101 + index, low=99 + index, volume=2000)
            for index in range(40)
        ]
        quote = _quote(price=140, prev_close=138, high=141, low=137, change_pct=1.45, turnover_rate=4.0)
        quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
        analysis = build_analysis(quote, klines, data_quality=quality)
        bundle = build_stock_insight_bundle(analysis)
        feature = build_feature_snapshot(analysis, bundle)
        chip = build_chip_analysis(analysis, feature)
        leadership = build_leadership_report(analysis, bundle, feature)
        factor_lab = build_factor_lab_report(analysis, bundle, feature, chip, leadership)
        weak_breadth = build_market_breadth_snapshot([
            _quote(price=10, prev_close=10.4, high=10.5, low=9.8, change_pct=-3.8)
            for index in range(8)
        ])
        warm_breadth = build_market_breadth_snapshot([
            _quote(price=10.5, prev_close=10, high=10.7, low=9.9, change_pct=5.0)
            for index in range(8)
        ])
        weak_regime = build_market_regime_report(analysis, bundle, feature, factor_lab, weak_breadth)
        warm_regime = build_market_regime_report(analysis, bundle, feature, factor_lab, warm_breadth)

        self.assertLess(weak_breadth.score, warm_breadth.score)
        self.assertGreaterEqual(weak_regime.risk_multiplier, warm_regime.risk_multiplier)
        self.assertIn("市场宽度", "；".join(weak_regime.evidence))

    def test_valuation_uses_price_and_history_anchor(self) -> None:
        klines = [
            _kline(date=f"2026-04-{(index % 28) + 1:02d}", close=100 + index, high=101 + index, low=99 + index, volume=2000)
            for index in range(40)
        ]
        quote = _quote(price=141, prev_close=139, high=142, low=138, change_pct=1.44, turnover_rate=4.0, pe=26.8, pb=2.95)
        history = [
            {"price": 120 + index, "change_pct": 0.5, "pe": 18 + index * 0.8, "pb": 2.2 + index * 0.06, "market_cap": 1_000_000_000}
            for index in range(32)
        ]
        quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
        analysis = build_analysis(quote, klines, data_quality=quality, quote_history=history)
        bundle = build_stock_insight_bundle(analysis)

        self.assertIsNotNone(bundle.valuation.price_percentile)
        self.assertIsNotNone(bundle.valuation.pe_percentile)
        self.assertIsNotNone(bundle.valuation.pb_percentile)
        self.assertIn("估值锚", bundle.valuation.valuation_anchor_label)
        self.assertIn("价格历史锚", "；".join(bundle.valuation.evidence))
        self.assertIn("PE历史锚", "；".join(bundle.valuation.evidence))

    def test_valuation_history_requires_distinct_trade_days(self) -> None:
        klines = [
            _kline(date=f"2026-04-{(index % 28) + 1:02d}", close=100 + index, high=101 + index, low=99 + index, volume=2000)
            for index in range(40)
        ]
        quote = _quote(price=141, prev_close=139, high=142, low=138, change_pct=1.44, turnover_rate=4.0, pe=26.8, pb=2.95)
        history = [
            {
                "price": 120 + index,
                "change_pct": 0.5,
                "pe": 18 + index * 0.8,
                "pb": 2.2 + index * 0.06,
                "market_cap": 1_000_000_000,
                "quote_timestamp": "2026-05-13 10:00:00",
                "fetched_at": f"2026-05-13 10:{index:02d}:00",
            }
            for index in range(32)
        ]
        quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
        analysis = build_analysis(quote, klines, data_quality=quality, quote_history=history)

        self.assertIsNone(_valuation_percentile_from_history(analysis, "pe"))

    def test_valuation_uses_peer_percentiles_when_peer_quotes_exist(self) -> None:
        klines = [
            _kline(date=f"2026-04-{(index % 28) + 1:02d}", close=100 + index, high=101 + index, low=99 + index, volume=2000)
            for index in range(40)
        ]
        quote = _quote(price=141, prev_close=139, high=142, low=138, change_pct=1.44, turnover_rate=4.0, pe=26.8, pb=2.95)
        peer_quotes = [
            Quote(
                code=f"600{index:03d}",
                name=f"同行{index}",
                market="SH",
                price=10 + index,
                prev_close=9.8 + index,
                open=9.9 + index,
                high=10.2 + index,
                low=9.7 + index,
                volume=100000,
                amount=1_000_000,
                change=0.2,
                change_pct=2.0,
                turnover_rate=1.5,
                pe=18 + index,
                pb=2.0 + index * 0.08,
                timestamp="2026-05-13 10:00:00",
                source="测试行情",
            )
            for index in range(18)
        ]
        quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
        analysis = build_analysis(quote, klines, data_quality=quality, peer_quotes=peer_quotes)
        bundle = build_stock_insight_bundle(analysis)

        self.assertIsNotNone(bundle.valuation.peer_pe_percentile)
        self.assertIsNotNone(bundle.valuation.peer_pb_percentile)
        self.assertEqual(bundle.valuation.peer_sample_count, 18)
        self.assertIn("同行PE分位", "；".join(bundle.valuation.evidence))

    def test_market_breadth_symbols_use_stock_pool_before_truncation(self) -> None:
        async def run_check(path: Path) -> list[str]:
            hub = _local_only_datahub(path)
            fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            hub.cache.save_stock_pool([
                _stock_info(code=f"0000{index:02d}", market="SZ").model_copy(update={"updated_at": fresh_time})
                for index in range(70)
            ])
            return await market_breadth_symbols(hub)

        with TemporaryDirectory() as tmpdir:
            symbols = __import__("asyncio").run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertGreater(len(symbols), len(Settings().seed_symbols))
        self.assertLessEqual(len(symbols), 60)

    def test_market_breadth_symbols_are_stratified_across_markets(self) -> None:
        class FakeHub:
            settings = Settings()

            async def stock_pool(self, limit: int = 1200, refresh: bool = False):
                return [
                    _stock_info(code=f"600{index:03d}", market="SH")
                    for index in range(240)
                ] + [
                    _stock_info(code=f"000{index:03d}", market="SZ")
                    for index in range(240)
                ]

        symbols = __import__("asyncio").run(market_breadth_symbols(FakeHub()))

        sh_count = sum(1 for item in symbols if item.endswith(".SH"))
        sz_count = sum(1 for item in symbols if item.endswith(".SZ"))
        self.assertGreaterEqual(sh_count, 20)
        self.assertGreaterEqual(sz_count, 20)
        self.assertGreaterEqual(len({item.split(".")[0] for item in symbols}), 40)

    def test_market_breadth_quotes_tolerate_partial_failures(self) -> None:
        class FakeHub:
            settings = Settings()

            async def stock_pool(self, limit: int = 1200, refresh: bool = False):
                return [_stock_info(code=f"600{index:03d}", market="SH") for index in range(40)]

            async def quotes(self, symbols, use_cache: bool = True):
                normalized = list(symbols)
                if len(normalized) > 1:
                    raise RuntimeError("batch failed")
                symbol = normalized[0]
                if symbol.endswith("005.SH"):
                    raise RuntimeError("single failed")
                code, market = symbol.split(".")
                return [
                    Quote(
                        code=code,
                        name=f"测试{code}",
                        market=market,
                        price=10.0,
                        prev_close=9.8,
                        open=9.9,
                        high=10.2,
                        low=9.7,
                        volume=100000,
                        amount=1_000_000,
                        change=0.2,
                        change_pct=2.0,
                        turnover_rate=1.5,
                        timestamp="2026-05-13 10:00:00",
                        source="测试行情",
                    )
                ]

        quotes = __import__("asyncio").run(market_breadth_quotes(FakeHub()))

        self.assertGreater(len(quotes), 10)
        self.assertTrue(all(item.code != "600005" for item in quotes))

    def test_sampling_quote_fallback_logs_failed_symbols(self) -> None:
        class FakeCache:
            def __init__(self) -> None:
                self.events: list[tuple[str, str]] = []

            def log_event(self, category: str, message: str) -> None:
                self.events.append((category, message))

        class FakeHub:
            def __init__(self) -> None:
                self.cache = FakeCache()

            async def quotes(self, symbols, use_cache: bool = True):
                normalized = list(symbols)
                if len(normalized) > 1:
                    raise RuntimeError("batch failed")
                symbol = normalized[0]
                if symbol == "600002.SH":
                    raise RuntimeError("single failed")
                code, market = symbol.split(".")
                return [
                    Quote(
                        code=code,
                        name=f"测试{code}",
                        market=market,
                        price=10.0,
                        prev_close=9.8,
                        open=9.9,
                        high=10.2,
                        low=9.7,
                        volume=100000,
                        amount=1_000_000,
                        change=0.2,
                        change_pct=2.0,
                        turnover_rate=1.5,
                        timestamp="2026-05-13 10:00:00",
                        source="测试行情",
                    )
                ]

        hub = FakeHub()
        quotes = __import__("asyncio").run(
            fetch_quotes_with_single_fallback(
                hub,
                ["600001.SH", "600002.SH", "600001.SH"],
                batch_size=3,
                context="测试样本",
            )
        )

        self.assertEqual([item.code for item in quotes], ["600001"])
        messages = "；".join(message for _, message in hub.cache.events)
        self.assertIn("测试样本剔除 1 个重复或无效样本", messages)
        self.assertIn("批量行情失败", messages)
        self.assertIn("600002.SH", messages)
        self.assertIn("最终缺失 1 / 2 个样本", messages)

    def test_strong_stock_watch_degrades_when_one_kline_fails(self) -> None:
        class FakeCache:
            def __init__(self) -> None:
                self.events: list[tuple[str, str]] = []

            def log_event(self, category: str, message: str) -> None:
                self.events.append((category, message))

        class FakeHub:
            def __init__(self) -> None:
                self.cache = FakeCache()

            async def quotes(self, symbols, use_cache: bool = True):
                result = []
                for symbol in symbols:
                    code, market = symbol.split(".")
                    result.append(
                        Quote(
                            code=code,
                            name=f"测试{code}",
                            market=market,
                            price=10.0,
                            prev_close=9.8,
                            open=9.9,
                            high=10.2,
                            low=9.7,
                            volume=100000,
                            amount=1_000_000,
                            change=0.2,
                            change_pct=2.0,
                            turnover_rate=1.5,
                            timestamp="2026-05-13 10:00:00",
                            source="测试行情",
                        )
                    )
                return result

            async def kline(self, symbol: str, limit: int = 80):
                if symbol == "600002.SH":
                    raise RuntimeError("kline failed")
                return [_kline(date="2026-05-13") for _ in range(limit)]

        hub = FakeHub()
        result = __import__("asyncio").run(
            workflow_strong_stock_watch(hub, Settings(), symbols="600001.SH,600002.SH")
        )

        self.assertEqual(result["sample_count"], 2)
        self.assertEqual([item.code for item in result["items"]], ["600001"])
        messages = "；".join(message for _, message in hub.cache.events)
        self.assertIn("K线失败", messages)
        self.assertIn("600002.SH", messages)
        self.assertIn("不参与强股排序", messages)

    def test_quote_history_persists_valuation_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            cache.save_quotes([_quote(pe=28.5, pb=4.2, market_cap=1_000_000_000)])
            rows = cache.quote_history("600519.SH", limit=5)

        self.assertEqual(rows[-1]["pe"], 28.5)
        self.assertEqual(rows[-1]["pb"], 4.2)
        self.assertEqual(rows[-1]["market_cap"], 1_000_000_000)

    def test_quote_history_returns_latest_snapshot_per_trade_date(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            cache.save_quotes([_quote(timestamp="2026-05-13 09:35:00", price=1300.0, pe=20.0)])
            cache.save_quotes([_quote(timestamp="2026-05-13 14:55:00", price=1305.0, pe=21.0)])
            cache.save_quotes([_quote(timestamp="2026-05-14 10:10:00", price=1308.0, pe=22.0)])
            rows = cache.quote_history("600519.SH", limit=2)

        self.assertEqual([item["price"] for item in rows], [1305.0, 1308.0])

    def test_price_alert_uses_quote_quality_gate(self) -> None:
        async def run_check(path: Path):
            cache = SQLiteCache(path)
            stale_quote = _quote(source="腾讯行情·缓存", timestamp="2000-01-03 10:00:00")
            cache.create_alert_rule(
                stale_quote,
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1200.0),
            )

            class FakeDataHub:
                def __init__(self) -> None:
                    self.cache = cache

                async def quote(self, symbol: str) -> Quote:
                    return stale_quote

                async def assess_quote_quality(self, quote: Quote, **kwargs):
                    return build_data_quality(quote, [], require_kline=False)

            return await evaluate_alert_rules(FakeDataHub(), symbol="600519")

        with TemporaryDirectory() as tmpdir:
            summary = __import__("asyncio").run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(summary.triggered_count, 1)
        self.assertIn("低置信提醒", summary.items[0].message)
        self.assertIn("报价严重滞后", summary.items[0].message)

    def test_alert_evaluation_isolates_recoverable_provider_failure_per_rule(self) -> None:
        async def run_check(path: Path):
            cache = SQLiteCache(path)
            first_quote = _quote()
            second_quote = first_quote.model_copy(update={"code": "000001", "market": "SZ", "name": "平安银行"})
            cache.create_alert_rule(
                first_quote,
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1200.0),
            )
            cache.create_alert_rule(
                second_quote,
                AlertRuleInput(symbol="000001", condition_type="price_above", threshold=10.0),
            )

            class FakeDataHub:
                def __init__(self) -> None:
                    self.cache = cache

                async def quote(self, symbol: str) -> Quote:
                    if symbol == "000001.SZ":
                        raise RuntimeError("quote provider unavailable")
                    return first_quote

                async def assess_quote_quality(self, quote: Quote, **kwargs):
                    return build_data_quality(quote, [], require_kline=False)

            return await evaluate_alert_rules(FakeDataHub())

        with TemporaryDirectory() as tmpdir:
            summary = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(summary.checked_count, 2)
        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(summary.triggered_count, 1)
        items_by_symbol = {item.rule.symbol: item for item in summary.items}
        self.assertEqual(items_by_symbol["600519.SH"].status, "evaluated")
        self.assertEqual(items_by_symbol["000001.SZ"].status, "failed")
        self.assertIn("quote provider unavailable", items_by_symbol["000001.SZ"].message)
