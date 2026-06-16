from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.schemas import AlertRuleInput, AlertRuleItem, AlertRuleUpdate, MinuteKline, Quote, StockConceptItem, StockNoteInput, StockNoteItem, StockNoteUpdate, StockQuestionAnswer
from app.services import research, trading_calendar
from app.services.cache import SQLiteCache
from app.services.alerts import _should_emit_event, evaluate_alert_rules, validate_alert_condition
from app.services.chart_marks import _note_marks
from app.services.analysis import build_analysis
from app.services.data_quality import assess_kline_quality, build_data_quality
from app.services.datahub import DataHub, _provider_error_text, _provider_source_key
from app.services.provider_registry import provider_priority
from app.services.indicators import recent_volume_ratio, support_resistance, trend_score, trend_score_snapshot
from app.services.llm_explainer import enhance_stock_answer
from app.services.market_sampling import market_breadth_quotes, market_breadth_symbols, unique_standard_symbols
from app.services.minute_analysis import build_minute_analysis_report
from app.services.optional_providers import _eastmoney_kline, _eastmoney_no_proxy, _eastmoney_quote_from_row, _merge_no_proxy
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
from app.services.stock_insights import RULE_VERSION, _valuation_percentile_from_history, build_stock_insight_bundle, rule_definitions
from app.services.workbench_context import WorkbenchContextCache
from app.config import Settings, _load_shell_env
from app.utils.errors import NotFoundError
from app.utils.time import now_text
from app.workflows import individual
from app.workflows.individual import stock_minute_analysis
from tests.factories import (
    make_kline as _kline,
    make_plate_item as _plate_item,
    make_quote as _quote,
    make_stock_info as _stock_info,
)


class RuleDefinitionTests(unittest.TestCase):
    def test_rule_definitions_are_versioned_and_parameterized(self) -> None:
        rules = rule_definitions()
        self.assertGreaterEqual(len(rules), 6)
        for rule in rules:
            self.assertEqual(rule.version, RULE_VERSION)
            self.assertTrue(rule.parameters, rule.id)


class AlertCooldownTests(unittest.TestCase):
    def test_trigger_emits_when_state_changes_or_cooldown_expires(self) -> None:
        rule = _alert_rule(last_state="未触发", last_triggered_at=None, cooldown_seconds=300)
        self.assertTrue(_should_emit_event(rule, True, "2026-05-13 10:00:00"))

        cooling = _alert_rule(last_state="触发", last_triggered_at="2026-05-13 09:59:00", cooldown_seconds=300)
        self.assertFalse(_should_emit_event(cooling, True, "2026-05-13 10:00:00"))

        expired = _alert_rule(last_state="触发", last_triggered_at="2026-05-13 09:54:30", cooldown_seconds=300)
        self.assertTrue(_should_emit_event(expired, True, "2026-05-13 10:00:00"))

        self.assertFalse(_should_emit_event(expired, False, "2026-05-13 10:01:00"))


class AlertValidationTests(unittest.TestCase):
    def test_price_alert_rejects_zero_threshold_but_dynamic_support_allows_zero(self) -> None:
        with self.assertRaises(ValueError):
            validate_alert_condition("price_above", 0)
        with self.assertRaises(ValueError):
            validate_alert_condition("trend_score_above", 101)
        with self.assertRaises(ValueError):
            validate_alert_condition("change_pct_below", -120)

        validate_alert_condition("break_support", 0)
        validate_alert_condition("break_resistance", 0)


class TradingCalendarTests(unittest.TestCase):
    def test_latest_expected_trade_date_uses_cached_trade_days(self) -> None:
        trade_days = {date(2026, 2, 13), date(2026, 2, 24)}
        with patch("app.services.trading_calendar._trade_days", return_value=trade_days):
            self.assertEqual(trading_calendar.latest_expected_trade_date(datetime(2026, 2, 24, 14, 0, 0)), date(2026, 2, 13))
            self.assertEqual(trading_calendar.expected_quote_date(datetime(2026, 2, 24, 9, 20, 0)), date(2026, 2, 24))
            self.assertEqual(trading_calendar.trading_day_gap(date(2026, 2, 13), date(2026, 2, 24)), 1)


class ChartMarkTests(unittest.TestCase):
    def test_note_marks_preserve_visibility_and_kline_date(self) -> None:
        marks = _note_marks(
            [
                StockNoteItem(
                    id=1,
                    symbol="600519.SH",
                    code="600519",
                    market="SH",
                    name="贵州茅台",
                    note_type="复盘",
                    content="回踩20日线后观察承接。",
                    price=1688.0,
                    trade_date="2026-05-13 10:30:00",
                    color="#2563eb",
                    visible=False,
                    created_at="2026-05-13 10:31:00",
                    updated_at="2026-05-13 10:31:00",
                )
            ]
        )
        self.assertEqual(marks[0].kline_date, "2026-05-13")
        self.assertEqual(marks[0].anchor_price_type, "manual")
        self.assertFalse(marks[0].visible)


class DataSourceReliabilityTests(unittest.TestCase):
    def test_demo_provider_is_not_in_default_realtime_priority(self) -> None:
        with TemporaryDirectory() as tmpdir:
            hub = DataHub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3"))
            quote_names = [name for _, name in provider_priority(Settings(), hub.providers, "quote")]
            kline_names = [name for _, name in provider_priority(Settings(), hub.providers, "kline")]

        self.assertNotIn("demo", quote_names)
        self.assertNotIn("demo", kline_names)

    def test_source_key_normalizes_cached_and_display_names(self) -> None:
        self.assertEqual(_provider_source_key("腾讯行情·缓存"), "tencent")
        self.assertEqual(_provider_source_key("AKShare"), "akshare")
        self.assertEqual(_provider_source_key("本地演示数据"), "demo")

    def test_single_source_consistency_does_not_self_compare(self) -> None:
        async def run_check(path: Path) -> tuple[str, list[str], int]:
            hub = DataHub(cache=SQLiteCache(path))
            with patch.object(hub, "_priority", return_value=[(1, "tencent")]):
                return await hub._quote_consistency(_quote(source="腾讯行情"))

        with TemporaryDirectory() as tmpdir:
            level, notes, penalty = __import__("asyncio").run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(level, "单源可用")
        self.assertGreaterEqual(penalty, 8)
        self.assertIn("多源一致性暂无法确认", "；".join(notes))

    def test_failed_provider_enters_short_cooldown_without_counting_skip_as_failure(self) -> None:
        class FailingProvider:
            source_name = "失败测试源"

            async def quotes(self, symbols):
                raise RuntimeError("network down")

        class BackupProvider:
            source_name = "备用测试源"

            async def quotes(self, symbols):
                return [_quote(source=self.source_name) for _ in symbols]

        async def run_check(path: Path) -> tuple[int, int]:
            hub = DataHub(cache=SQLiteCache(path))
            hub.settings.provider_failure_cooldown_seconds = 60
            hub.providers["broken"] = FailingProvider()
            hub.providers["backup"] = BackupProvider()
            with patch.object(hub, "_priority", return_value=[(1, "broken"), (2, "backup")]):
                await hub.quotes(["600519.SH"], use_cache=False)
                first_failure_count = next(item.failure_count for item in hub.cache.provider_statuses() if item.name == "broken")
                await hub.quotes(["600519.SH"], use_cache=False)
                second_failure_count = next(item.failure_count for item in hub.cache.provider_statuses() if item.name == "broken")
            return first_failure_count, second_failure_count

        with TemporaryDirectory() as tmpdir:
            first, second = __import__("asyncio").run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(first, 1)
        self.assertEqual(second, 1)

    def test_quotes_merges_partial_provider_results_with_backup_source(self) -> None:
        def quote_for(symbol: str, source: str) -> Quote:
            code, market = symbol.split(".")
            return _quote(source=source).model_copy(update={"code": code, "market": market, "name": f"测试{code}"})

        class PartialProvider:
            source_name = "部分测试源"

            async def quotes(self, symbols):
                return [quote_for(symbols[0], self.source_name)] if symbols else []

        class BackupProvider:
            source_name = "补齐测试源"

            async def quotes(self, symbols):
                return [quote_for(symbol, self.source_name) for symbol in symbols]

        async def run_check(path: Path) -> list[Quote]:
            hub = DataHub(cache=SQLiteCache(path))
            hub.providers["partial"] = PartialProvider()
            hub.providers["backup"] = BackupProvider()
            with patch.object(hub, "_priority", return_value=[(1, "partial"), (2, "backup")]):
                return await hub.quotes(["600519.SH", "000001.SZ"], use_cache=False)

        with TemporaryDirectory() as tmpdir:
            rows = __import__("asyncio").run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual([item.code for item in rows], ["600519", "000001"])
        self.assertEqual(rows[0].source, "部分测试源")
        self.assertEqual(rows[1].source, "补齐测试源")

    def test_provider_failure_records_timeout_class_when_message_is_empty(self) -> None:
        self.assertEqual(_provider_error_text(TimeoutError()), "TimeoutError: 数据源响应超时")

    def test_akshare_eastmoney_requests_bypass_system_proxy(self) -> None:
        merged = _merge_no_proxy("localhost,example.com")
        self.assertIn("localhost", merged)
        self.assertIn("eastmoney.com", merged)
        self.assertIn("push2his.eastmoney.com", merged)

        with patch.dict("os.environ", {"NO_PROXY": "localhost", "no_proxy": "localhost"}, clear=False):
            with _eastmoney_no_proxy():
                self.assertIn("eastmoney.com", __import__("os").environ["NO_PROXY"])
                self.assertIn("push2his.eastmoney.com", __import__("os").environ["no_proxy"])
            self.assertEqual(__import__("os").environ["NO_PROXY"], "localhost")

    def test_eastmoney_light_quote_maps_to_quote_model(self) -> None:
        quote = _eastmoney_quote_from_row(
            {
                "f2": 1303.0,
                "f3": 2.33,
                "f4": 29.62,
                "f5": 82728,
                "f6": 10586574902.0,
                "f8": 0.66,
                "f9": 14.97,
                "f12": "600519",
                "f14": "贵州茅台",
                "f15": 1319.0,
                "f16": 1250.1,
                "f17": 1268.02,
                "f18": 1273.38,
                "f20": 1600000000000,
                "f23": 6.8,
            }
        )

        self.assertEqual(quote.code, "600519")
        self.assertEqual(quote.market, "SH")
        self.assertEqual(quote.name, "贵州茅台")
        self.assertEqual(quote.price, 1303.0)
        self.assertEqual(quote.source, "AKShare")

    def test_eastmoney_kline_fallback_parses_json_rows(self) -> None:
        payload = {"rc": 0, "data": {"klines": ["2026-05-27,1268.02,1303.00,1319.00,1250.10,82728,10586574902.00,5.41,2.33,29.62,0.66"]}}
        with patch("app.services.optional_providers._eastmoney_get_json", return_value=payload):
            rows = _eastmoney_kline("600519", period="101", limit=5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].date, "2026-05-27")
        self.assertEqual(rows[0].close, 1303.0)

    def test_akshare_quotes_falls_back_to_original_loader_after_direct_failure(self) -> None:
        import pandas as pd

        provider = __import__("app.services.optional_providers", fromlist=["AKShareProvider"]).AKShareProvider()
        frame = pd.DataFrame(
            [
                {
                    "代码": "600519",
                    "名称": "贵州茅台",
                    "最新价": 1303.0,
                    "昨收": 1273.38,
                    "涨跌额": 29.62,
                    "涨跌幅": 2.33,
                    "成交量": 82728,
                    "成交额": 10586574902.0,
                    "今开": 1268.02,
                    "最高": 1319.0,
                    "最低": 1250.1,
                }
            ]
        )

        class FakeAk:
            @staticmethod
            def stock_zh_a_spot_em():
                return frame

        with patch("app.services.optional_providers._eastmoney_quotes", side_effect=RuntimeError("direct failed")), patch(
            "app.services.optional_providers.is_installed", return_value=True
        ), patch.dict("sys.modules", {"akshare": FakeAk}):
            rows = asyncio.run(provider.quotes(["600519.SH"]))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].price, 1303.0)

    def test_akshare_plate_failure_does_not_mark_main_provider_failed(self) -> None:
        class FailingPlateProvider:
            source_name = "AKShare"

            async def plate_rank(self, limit: int = 20):
                raise RuntimeError("board remote disconnected")

        class LocalPlateProvider:
            source_name = "本地基础数据"

            async def plate_rank(self, limit: int = 20):
                return [_plate_item()]

        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            hub.providers["akshare"] = FailingPlateProvider()
            hub.providers["local"] = LocalPlateProvider()
            hub.cache.update_provider_success("akshare", 2, 12.0)
            with patch.object(hub, "_priority", return_value=[(2, "akshare"), (5, "local")]):
                rows = await hub.plate_rank(limit=1, refresh=True)
            status = next(item for item in hub.cache.provider_statuses() if item.name == "akshare")
            return rows, status

        with TemporaryDirectory() as tmpdir:
            rows, status = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(len(rows), 1)
        self.assertTrue(status.healthy)

    def test_data_status_exposes_source_plan(self) -> None:
        with TemporaryDirectory() as tmpdir:
            hub = DataHub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3"))
            hub.cache.update_provider_success("tencent", 1, 12.0)
            hub.cache.update_provider_failure("akshare", 2, "ProxyError")
            status = hub.status()

        self.assertIsNotNone(status.source_plan)
        assert status.source_plan is not None
        self.assertEqual(status.source_plan.primary_quote_source, "tencent")
        self.assertTrue(status.source_plan.decisions)
        self.assertTrue(any(item.name == "akshare" and item.state in {"最近失败", "冷却中"} for item in status.source_plan.decisions))

    def test_capability_failure_does_not_cool_other_capability(self) -> None:
        with TemporaryDirectory() as tmpdir:
            hub = DataHub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3"))
            hub._record_provider_success("tencent", 1, 12.0, "quote")
            hub._record_provider_failure("tencent", 1, RuntimeError("kline down"), "kline")
            status = hub.status()

        capability_map = {(item.name, item.kind): item for item in status.capability_statuses}
        self.assertTrue(capability_map[("tencent", "quote")].healthy)
        self.assertFalse(capability_map[("tencent", "kline")].healthy)
        self.assertEqual(status.source_plan.primary_quote_source, "tencent")
        self.assertFalse(hub._provider_is_cooling("tencent", "quote"))
        self.assertTrue(hub._provider_is_cooling("tencent", "kline"))

    def test_provider_aggregate_counts_use_capability_counts_when_present(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            cache.update_provider_failure("tencent", 1, "legacy quote failures")
            cache.update_provider_capability_success("tencent", "quote", 1, 12.0)
            status = next(item for item in cache.provider_statuses() if item.name == "tencent")

        self.assertTrue(status.healthy)
        self.assertEqual(status.success_count, 1)
        self.assertEqual(status.failure_count, 0)
        self.assertIsNone(status.last_error)

    def test_short_cache_quote_still_runs_consistency_check(self) -> None:
        class BackupProvider:
            source_name = "备用行情"

            async def quotes(self, symbols):
                return [_quote(source=self.source_name) for _ in symbols]

        async def run_check(path: Path) -> tuple[str, str]:
            hub = DataHub(cache=SQLiteCache(path))
            hub.cache.save_quotes([_quote(source="腾讯行情")])
            hub.providers["backup"] = BackupProvider()
            quote = (await hub.quotes(["600519.SH"]))[0]
            with patch.object(hub, "_priority", return_value=[(1, "tencent"), (2, "backup")]):
                level, _, _ = await hub._quote_consistency(quote)
            return quote.source, level

        with TemporaryDirectory() as tmpdir:
            source, level = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertIn("短时缓存", source)
        self.assertEqual(level, "一致")

    def test_quotes_reuses_partial_cache_and_fetches_only_missing_symbols(self) -> None:
        class MissingOnlyProvider:
            source_name = "实时补齐源"

            def __init__(self) -> None:
                self.requested: list[str] = []

            async def quotes(self, symbols):
                self.requested.extend(symbols)
                return [
                    _quote(source=self.source_name).model_copy(update={"code": symbol.split(".")[0], "market": symbol.split(".")[1]})
                    for symbol in symbols
                ]

        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            hub.cache.save_quotes([_quote(source="腾讯行情")])
            provider = MissingOnlyProvider()
            hub.providers["missing_only"] = provider
            with patch.object(hub, "_priority", return_value=[(1, "missing_only")]):
                rows = await hub.quotes(["600519.SH", "000001.SZ"])
            return rows, provider.requested

        with TemporaryDirectory() as tmpdir:
            rows, requested = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(requested, ["000001.SZ"])
        self.assertIn("短时缓存", rows[0].source)
        self.assertEqual(rows[1].source, "实时补齐源")

    def test_quote_with_quality_use_cache_false_fetches_live_quote_and_still_checks_consistency(self) -> None:
        class LiveProvider:
            source_name = "实时测试源"

            async def quotes(self, symbols):
                return [_quote(source=self.source_name).model_copy(update={"code": symbol.split(".")[0], "market": symbol.split(".")[1]}) for symbol in symbols]

        async def run_check(path: Path) -> tuple[str, str]:
            hub = DataHub(cache=SQLiteCache(path))
            hub.cache.save_quotes([_quote(source="腾讯行情")])
            hub.providers["live"] = LiveProvider()
            with patch.object(hub, "_priority", return_value=[(1, "live")]):
                quote, quality = await hub.quote_with_quality("600519.SH", use_cache=False)
            return quote.source, quality.consistency_level

        with TemporaryDirectory() as tmpdir:
            source, consistency_level = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(source, "实时测试源")
        self.assertNotEqual(consistency_level, "未校验")

    def test_warmup_forces_quote_and_kline_refresh(self) -> None:
        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            calls = []

            async def fake_quotes(symbols, use_cache: bool = True):
                calls.append(("quotes", use_cache, tuple(symbols)))
                return []

            async def fake_kline(symbol, limit: int = 120, use_cache: bool = True):
                calls.append(("kline", use_cache, symbol, limit))
                return []

            with patch.object(hub, "quotes", side_effect=fake_quotes), patch.object(hub, "kline", side_effect=fake_kline):
                await hub.warmup(["600519.SH", "000001.SZ"])
            return calls

        with TemporaryDirectory() as tmpdir:
            calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertIn(("quotes", False, ("600519.SH", "000001.SZ")), calls)
        self.assertEqual([item for item in calls if item[0] == "kline"], [("kline", False, "600519.SH", 120), ("kline", False, "000001.SZ", 120)])

    def test_incomplete_stock_pool_miss_is_not_treated_as_not_found(self) -> None:
        class TinyStockPoolProvider:
            source_name = "小样本股票池"

            async def stock_pool(self):
                return [_stock_info(code="600519", market="SH")]

        async def run_check(path: Path) -> str:
            hub = DataHub(cache=SQLiteCache(path))
            hub.providers["tiny"] = TinyStockPoolProvider()
            with patch.object(hub, "_priority", return_value=[(1, "tiny")]):
                try:
                    await individual._confirmed_stock_profile(hub, "688001.SH")
                except Exception as exc:
                    return exc.__class__.__name__
            return "ok"

        with TemporaryDirectory() as tmpdir:
            error_name = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(error_name, "RuntimeError")

    def test_authoritative_stock_pool_miss_is_not_found(self) -> None:
        async def run_check(path: Path) -> str:
            hub = DataHub(cache=SQLiteCache(path))
            fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            hub.cache.save_stock_pool(
                [
                    _stock_info(code=f"60{index:04d}", market="SH").model_copy(update={"updated_at": fresh_time})
                    for index in range(1000)
                ]
            )
            try:
                await individual._confirmed_stock_profile(hub, "000000.SH")
            except Exception as exc:
                return exc.__class__.__name__
            return "ok"

        with TemporaryDirectory() as tmpdir:
            error_name = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(error_name, NotFoundError.__name__)

    def test_stale_stock_master_match_is_not_reported_as_missing(self) -> None:
        async def run_check(path: Path) -> tuple[list[str], str | None]:
            hub = DataHub(cache=SQLiteCache(path))
            fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            stale_time = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
            fresh_rows = [
                _stock_info(code=f"60{index:04d}", market="SH").model_copy(update={"updated_at": fresh_time})
                for index in range(10)
            ]
            stale_match = _stock_info(code="002182", market="SZ").model_copy(
                update={"name": "宝武镁业", "updated_at": stale_time}
            )
            hub.cache.save_stock_pool([*fresh_rows, stale_match])

            rows = await hub.stock_pool(keyword="002182", limit=10)
            profile = await hub.stock_profile("002182.SZ")
            return [item.symbol for item in rows], profile.symbol if profile else None

        with TemporaryDirectory() as tmpdir:
            rows, profile_symbol = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(rows, ["002182.SZ"])
        self.assertEqual(profile_symbol, "002182.SZ")


class LlmExplainerTests(unittest.TestCase):
    def test_llm_shell_env_loads_llm_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".zshrc"
            path.write_text(
                "\n".join(
                    [
                        "# AShareRadar LLM configuration",
                        "export ASHARE_RADAR_LLM_API_KEY=' file-key '",
                        'export ASHARE_RADAR_LLM_BASE_URL="https://example.test/v1"',
                        "export ASHARE_RADAR_LLM_MODEL='test-model'",
                        "export ASHARE_RADAR_LLM_ENABLED=1",
                        "export ASHARE_RADAR_LLM_TIMEOUT_SECONDS=3",
                    ]
                ),
                encoding="utf-8",
            )

            values = _load_shell_env(
                path,
                {
                    "ASHARE_RADAR_LLM_API_KEY",
                    "ASHARE_RADAR_LLM_BASE_URL",
                    "ASHARE_RADAR_LLM_MODEL",
                    "ASHARE_RADAR_LLM_ENABLED",
                    "ASHARE_RADAR_LLM_TIMEOUT_SECONDS",
                },
            )

        self.assertEqual(values["ASHARE_RADAR_LLM_API_KEY"], "file-key")
        self.assertEqual(values["ASHARE_RADAR_LLM_BASE_URL"], "https://example.test/v1")
        self.assertEqual(values["ASHARE_RADAR_LLM_MODEL"], "test-model")
        self.assertEqual(values["ASHARE_RADAR_LLM_ENABLED"], "1")
        self.assertEqual(values["ASHARE_RADAR_LLM_TIMEOUT_SECONDS"], "3")

    def test_llm_explainer_falls_back_without_api_key(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = Settings(llm_enabled=True, llm_api_key=None)

        result = asyncio.run(enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis))

        self.assertEqual(result.answer, rule_answer.answer)
        self.assertEqual(result.answer_source, "规则问诊")
        self.assertFalse(result.llm_used)
        self.assertEqual(result.llm_status, "未配置大模型API")

    def test_llm_explainer_uses_grounded_model_answer(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = Settings(llm_enabled=True, llm_api_key="test-key", llm_model="deepseek-v4-flash")
        llm_text = f"结论：先观察。为什么：现价 {analysis.quote.price:.2f}，高于支撑 {analysis.support:.2f}，压力 {analysis.resistance:.2f} 未突破。接下来盯什么：看20日线和量能。失效条件：跌破 {analysis.support:.2f}。"

        with patch("app.services.llm_explainer._call_llm", return_value=llm_text):
            result = asyncio.run(enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis))

        self.assertEqual(result.answer, llm_text)
        self.assertTrue(result.llm_used)
        self.assertIn("deepseek-v4-flash", result.answer_source)
        self.assertEqual(result.llm_status, "已基于当前分析结果生成解释")

    def test_llm_explainer_rejects_ungrounded_numbers(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = Settings(llm_enabled=True, llm_api_key="test-key")

        with patch("app.services.llm_explainer._call_llm", return_value="结论：可以等 1888 元突破后再看，这是当前关键位。"):
            result = asyncio.run(enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis))

        self.assertEqual(result.answer, rule_answer.answer)
        self.assertFalse(result.llm_used)
        self.assertIn("事实校验", result.llm_status or "")


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
        self.assertGreaterEqual(report.sample_count, 30)
        self.assertTrue(report.supports)
        self.assertTrue(report.resistances)
        self.assertIn(report.t_plan.suitability, {"仅底仓可做T", "等待更大区间", "不适合主动做T"})
        self.assertTrue(report.t_plan.execution_steps)
        self.assertTrue(report.t_plan.stop_conditions)

    def test_minute_analysis_returns_safe_report_when_source_fails(self) -> None:
        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            with patch.object(hub, "minute_kline", side_effect=RuntimeError("所有分钟K线数据源均不可用：akshare: ProxyError('Unable to connect to proxy')")):
                return await stock_minute_analysis(hub, "600900", interval="5m", limit=120)

        with TemporaryDirectory() as tmpdir:
            report = __import__("asyncio").run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(report.symbol, "600900.SH")
        self.assertEqual(report.t_plan.suitability, "不适合主动做T")
        self.assertIn("分钟K线", report.missing_data)
        self.assertIn("网络代理连接失败", report.summary)

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

    def test_recent_volume_ratio_is_stable(self) -> None:
        klines = [_kline(volume=1000 + index * 20) for index in range(30)]
        self.assertGreater(recent_volume_ratio(klines), 1.0)

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
        klines = [
            _kline(date=f"2026-05-{index + 1:02d}", close=100 + index, high=101 + index, low=99 + index, volume=2000)
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
        self.assertLess(theme_answer_without_context.confidence, theme_answer.confidence)
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
            hub = DataHub(cache=SQLiteCache(path))
            hub.cache.save_stock_pool([
                _stock_info(code=f"0000{index:02d}", market="SZ")
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
            with cache._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO quote_history (
                        symbol, code, market, name, price, change_pct, pe, pb, market_cap, source,
                        quote_timestamp, trade_date, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("600519.SH", "600519", "SH", "贵州茅台", 100.0, 0.1, 20.0, 3.0, 1.0, "测试", "2026-05-13 09:35:00", "2026-05-13", "2026-05-13 09:35:01"),
                        ("600519.SH", "600519", "SH", "贵州茅台", 105.0, 0.5, 21.0, 3.2, 1.1, "测试", "2026-05-13 14:55:00", "2026-05-13", "2026-05-13 14:55:01"),
                        ("600519.SH", "600519", "SH", "贵州茅台", 108.0, 0.8, 22.0, 3.4, 1.2, "测试", "2026-05-14 10:10:00", "2026-05-14", "2026-05-14 10:10:01"),
                    ],
                )
            rows = cache.quote_history("600519.SH", limit=2)

        self.assertEqual([item["price"] for item in rows], [105.0, 108.0])

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


class LocalLifecycleTests(unittest.TestCase):
    def test_alert_rule_can_be_updated_without_losing_identity(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()
            created = cache.create_alert_rule(
                quote,
                AlertRuleInput(
                    symbol="600519",
                    condition_type="price_above",
                    threshold=1300.0,
                    note="初始提醒",
                ),
            )

            updated = cache.update_alert_rule(
                created.id,
                AlertRuleUpdate(name="关键压力观察", threshold=1350.5, note=None, enabled=False, cooldown_seconds=900),
            )

            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.id, created.id)
            self.assertEqual(updated.name, "关键压力观察")
            self.assertEqual(updated.threshold, 1350.5)
            self.assertIsNone(updated.note)
            self.assertFalse(updated.enabled)
            self.assertEqual(updated.cooldown_seconds, 900)

    def test_stock_note_can_toggle_visibility_and_clear_anchor(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()
            created = cache.create_stock_note(
                quote,
                StockNoteInput(
                    symbol="600519",
                    content="回踩后观察承接。",
                    note_type="观察",
                    price=1288.0,
                    trade_date="2026-05-13 10:00:00",
                    visible=True,
                ),
            )

            updated = cache.update_stock_note(
                created.id,
                StockNoteUpdate(content="改为只保留复盘，不上图。", note_type="复盘", price=None, visible=False),
            )

            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.id, created.id)
            self.assertEqual(updated.content, "改为只保留复盘，不上图。")
            self.assertEqual(updated.note_type, "复盘")
            self.assertIsNone(updated.price)
            self.assertFalse(updated.visible)
            self.assertEqual(cache.stock_notes("600519", visible_only=True), [])

    def test_runtime_cleanup_handles_minute_kline_table_without_id(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            rows = [
                MinuteKline(
                    timestamp=f"2026-05-15 10:{index:02d}:00",
                    open=100 + index,
                    close=100 + index,
                    high=101 + index,
                    low=99 + index,
                    volume=1000 + index,
                    interval="5m",
                    source="测试分钟线",
                )
                for index in range(6)
            ]
            cache.save_minute_klines("600519.SH", "5m", rows, "测试分钟线")
            removed = cache.cleanup_runtime_rows()
            counts = cache.table_counts()

        self.assertIn("kline_minute", removed)
        self.assertGreaterEqual(counts["kline_minute"], 1)

    def test_monitor_events_merge_repeated_messages_after_old_window(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            cache.save_monitor_event("info", "quote", "刷新行情完成：10 只")
            with cache._connect() as conn:
                conn.execute(
                    "UPDATE monitor_event SET created_at = ?, last_seen_at = ? WHERE category = ?",
                    ("2026-05-13 09:00:00", "2026-05-13 09:00:00", "quote"),
                )
            cache.save_monitor_event("info", "quote", "刷新行情完成：10 只")
            events = cache.recent_monitor_events(limit=10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].created_at, "2026-05-13 09:00:00")
        self.assertNotEqual(events[0].last_seen_at, "2026-05-13 09:00:00")
        self.assertEqual(events[0].repeat_count, 2)

    def test_monitor_event_cleanup_keeps_recent_last_seen_event(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            cache.save_monitor_event("info", "quote", "较早创建但最近重复")
            cache.save_monitor_event("info", "kline", "较晚创建但不活跃")
            with cache._connect() as conn:
                conn.execute(
                    "UPDATE monitor_event SET created_at = ?, last_seen_at = ? WHERE category = ?",
                    ("2026-05-13 09:00:00", "2026-05-13 12:00:00", "quote"),
                )
                conn.execute(
                    "UPDATE monitor_event SET created_at = ?, last_seen_at = ? WHERE category = ?",
                    ("2026-05-13 10:00:00", "2026-05-13 10:00:00", "kline"),
                )
            with patch("app.repositories.maintenance.get_settings", return_value=Settings(max_monitor_event_rows=1)):
                cache.cleanup_runtime_rows()
            events = cache.recent_monitor_events(limit=10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].category, "quote")


class ThemeContextTests(unittest.TestCase):
    def test_stock_concept_schema_backfills_match_reason_for_old_cache(self) -> None:
        from app.db.schema import initialize_schema

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "legacy.sqlite3"
            import sqlite3

            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                CREATE TABLE stock_concept (
                    symbol TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    change_pct REAL NOT NULL DEFAULT 0,
                    amount REAL,
                    turnover_rate REAL,
                    leading_stock TEXT,
                    leading_stock_change_pct REAL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, name)
                )
                """
            )
            initialize_schema(conn)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(stock_concept)").fetchall()}
            conn.close()

        self.assertIn("match_reason", columns)

    def test_stock_concept_cache_roundtrip_and_cleanup(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            cache.save_stock_concepts(
                "600519.SH",
                [
                    StockConceptItem(
                        symbol="600519.SH",
                        rank=1,
                        name="白酒概念",
                        change_pct=1.8,
                        amount=2_000_000_000,
                        turnover_rate=2.1,
                        leading_stock="贵州茅台",
                        leading_stock_change_pct=2.3,
                        match_reason="测试概念成分匹配",
                        source="测试概念源",
                        updated_at=now_text(),
                    )
                ],
            )
            rows = cache.get_stock_concepts("600519", max_age_seconds=60 * 60 * 24, limit=5)
            removed = cache.cleanup_runtime_rows()
            counts = cache.table_counts()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "白酒概念")
        self.assertIn("stock_concept", removed)
        self.assertGreaterEqual(counts["stock_concept"], 1)

    def test_theme_context_explains_relative_strength(self) -> None:
        analysis = build_analysis(
            _quote(change_pct=3.2),
            [_kline(date=f"2026-05-{index + 1:02d}", close=100 + index, high=101 + index, low=99 + index, volume=2000) for index in range(40)],
            stock_profile=_stock_info(),
            industry_context=_plate_item(change_pct=1.1),
            data_quality=build_data_quality(_quote(change_pct=3.2), [_kline() for _ in range(40)]),
        )
        insights = build_stock_insight_bundle(analysis)
        feature = build_feature_snapshot(analysis, insights)
        report = build_theme_context_report(
            analysis,
            feature,
            [
                StockConceptItem(
                    symbol="600519.SH",
                    rank=1,
                    name="白酒概念",
                    change_pct=1.6,
                    leading_stock="贵州茅台",
                    source="测试概念源",
                    updated_at="2026-05-15 10:00:00",
                )
            ],
        )

        self.assertIn(report.level, {"主题顺风", "主题配合"})
        self.assertIn("个股", report.style)
        self.assertIn(report.relative_strength, {"显著强于背景", "强于背景", "与背景同步"})
        self.assertTrue(any("相对" in item for item in report.evidence))

    def test_theme_context_handles_missing_concepts_conservatively(self) -> None:
        analysis = build_analysis(
            _quote(change_pct=-0.8),
            [_kline(date=f"2026-05-{index + 1:02d}", close=100 - index * 0.2, high=101, low=98, volume=1000) for index in range(40)],
            stock_profile=_stock_info(),
            industry_context=None,
            data_quality=build_data_quality(_quote(change_pct=-0.8), [_kline() for _ in range(40)]),
        )
        insights = build_stock_insight_bundle(analysis)
        feature = build_feature_snapshot(analysis, insights)
        report = build_theme_context_report(analysis, feature, [])

        self.assertEqual(report.level, "主题待确认")
        self.assertEqual(report.relative_strength, "强弱待确认")
        self.assertIn("概念归属成分", report.missing_data)
        self.assertTrue(any("保守" in item or "暂未" in item for item in report.risks + report.opportunities))

    def test_event_digest_exposes_external_data_checklist(self) -> None:
        analysis = build_analysis(
            _quote(change_pct=7.2, turnover_rate=13.5),
            [_kline(date=f"2026-05-{index + 1:02d}", close=100 + index * 0.4, high=102 + index * 0.4, low=99 + index * 0.4, volume=1000 + index * 120) for index in range(40)],
            data_quality=build_data_quality(_quote(change_pct=7.2, turnover_rate=13.5), [_kline() for _ in range(40)]),
        )
        insights = build_stock_insight_bundle(analysis)

        self.assertTrue(insights.lhb.action_items)
        self.assertIn("龙虎榜席位", insights.events.missing_sources)
        self.assertTrue(any(item.category in {"龙虎榜", "公告", "融资融券"} for item in insights.events.events))
        self.assertTrue(any(item.action_hint for item in insights.events.events))


class WorkbenchCacheTests(unittest.TestCase):
    def test_context_cache_trims_oldest_entries(self) -> None:
        cache = WorkbenchContextCache(max_size=32)
        for index in range(cache.max_size + 4):
            cache.entries[f"{index:06d}.SH"] = (float(index), None)  # type: ignore[assignment]

        cache.trim()

        self.assertEqual(len(cache.entries), cache.max_size)
        self.assertNotIn("000000.SH", cache.entries)
        self.assertIn(f"{cache.max_size + 3:06d}.SH", cache.entries)

    def test_stock_workbench_does_not_repeat_advice_snapshot_for_cached_context(self) -> None:
        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            quote = _quote(pe=26.8, pb=2.95, market_cap=1_000_000_000)
            klines = [
                _kline(date=f"2026-05-{index + 1:02d}", close=100 + index, high=101 + index, low=99 + index, volume=2000)
                for index in range(40)
            ]
            quality = build_data_quality(quote, klines)
            pool = [_stock_info(code="600519", market="SH")] + [_stock_info(code=f"600{index:03d}", market="SH") for index in range(20)]

            async def quotes_for(symbols, use_cache: bool = True):
                rows = []
                for idx, symbol in enumerate(symbols):
                    code, market = symbol.split(".")
                    rows.append(
                        quote.model_copy(
                            update={
                                "code": code,
                                "market": market,
                                "name": f"测试{code}",
                                "price": quote.price + idx,
                                "source": "测试行情",
                            }
                        )
                    )
                return rows

            hub.workbench_contexts = WorkbenchContextCache()
            with patch.object(hub, "quote", return_value=quote), patch.object(
                hub,
                "kline",
                return_value=klines,
            ), patch.object(
                hub,
                "plate_rank",
                return_value=[_plate_item()],
            ), patch.object(
                hub,
                "assess_quote_quality",
                return_value=quality,
            ), patch.object(
                hub,
                "stock_profile",
                return_value=_stock_info(code="600519", market="SH"),
            ), patch.object(
                hub,
                "stock_pool",
                return_value=pool,
            ), patch.object(
                hub,
                "quotes",
                side_effect=quotes_for,
            ), patch.object(
                hub,
                "stock_concepts",
                return_value=[],
            ):
                await individual.stock_workbench(hub, "600519")
                await individual.stock_workbench(hub, "600519")
            return hub.cache.advice_history("600519.SH", limit=5)

        with TemporaryDirectory() as tmpdir:
            history = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].repeat_count, 1)

    def test_strong_stock_symbol_sampling_deduplicates_normalized_symbols(self) -> None:
        rows = unique_standard_symbols(["000333", "000333.SZ", "SZ000333", "600036", "600036.SH"])
        self.assertEqual(rows, ["000333.SZ", "600036.SH"])

    def test_analyze_individual_stock_loads_peer_quotes(self) -> None:
        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            hub.cache.save_stock_pool(
                [_stock_info(code="600519", market="SH")]
                + [_stock_info(code=f"600{index:03d}", market="SH") for index in range(20)]
            )
            hub.cache.save_plate_rank([])
            peer_pool = [_stock_info(code="600519", market="SH")] + [_stock_info(code=f"600{index:03d}", market="SH") for index in range(20)]
            with patch.object(hub, "quote", return_value=_quote(pe=26.8, pb=2.95, market_cap=1_000_000_000)), patch.object(
                hub,
                "stock_profile",
                return_value=_stock_info(code="600519", market="SH"),
            ), patch.object(
                hub,
                "stock_pool",
                return_value=peer_pool,
            ), patch.object(
                hub,
                "kline",
                return_value=[_kline(date=f"2026-05-{index + 1:02d}", close=100 + index, high=101 + index, low=99 + index, volume=2000) for index in range(40)],
            ), patch.object(hub, "assess_quote_quality", return_value=build_data_quality(_quote(), [_kline() for _ in range(40)])), patch.object(
                hub,
                "quotes",
                side_effect=lambda symbols, use_cache=True: [
                    Quote(
                        code=item.split(".")[0],
                        name=f"同行{idx}",
                        market=item.split(".")[1],
                        price=10 + idx,
                        prev_close=9.8 + idx,
                        open=9.9 + idx,
                        high=10.2 + idx,
                        low=9.7 + idx,
                        volume=100000,
                        amount=1_000_000,
                        change=0.2,
                        change_pct=2.0,
                        turnover_rate=1.5,
                        pe=18 + idx,
                        pb=2.0 + idx * 0.08,
                        market_cap=1_000_000_000,
                        timestamp="2026-05-13 10:00:00",
                        source="测试行情",
                    )
                    for idx, item in enumerate(symbols)
                ],
            ):
                return await individual.analyze_individual_stock(hub, "600519", persist_history=False)

        with TemporaryDirectory() as tmpdir:
            analysis = __import__("asyncio").run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertGreaterEqual(len(analysis.peer_quotes), 5)


class ReplayConfidenceTests(unittest.TestCase):
    def test_replay_pattern_note_warns_when_sample_is_small(self) -> None:
        note = research._replay_pattern_note("放量突破", 3, 80.0, 3.2)
        self.assertIn("样本只有 3 次", note)
        self.assertIn("不宜提高权重", note)


def _alert_rule(
    *,
    last_state: str,
    last_triggered_at: str | None,
    cooldown_seconds: int,
) -> AlertRuleItem:
    return AlertRuleItem(
        id=1,
        symbol="600519.SH",
        code="600519",
        market="SH",
        stock_name="贵州茅台",
        name="测试预警",
        condition_type="price_above",
        condition_label="价格高于",
        threshold=1.0,
        enabled=True,
        last_checked_at=None,
        last_triggered_at=last_triggered_at,
        last_state=last_state,
        trigger_count=0,
        cooldown_seconds=cooldown_seconds,
        created_at="2026-05-13 09:00:00",
        updated_at="2026-05-13 09:00:00",
    )


def _llm_test_case() -> tuple:
    klines = [
        _kline(date=f"2026-05-{index + 1:02d}", close=1260 + index * 2.0, high=1262 + index * 2.0, low=1258 + index * 2.0, volume=2000 + index * 50)
        for index in range(25)
    ]
    quote = _quote(price=1300.0, prev_close=1290.0, high=1310.0, low=1288.0, change_pct=0.78)
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 10, 0, 0))
    analysis = build_analysis(quote, klines, data_quality=quality)
    rule_answer = StockQuestionAnswer(
        symbol="600519.SH",
        updated_at=quote.timestamp,
        question="现在能不能买？",
        topic="买点",
        conclusion="等待确认",
        answer="规则结论：当前更适合等待确认，不追高。",
        confidence=68,
        evidence=[f"现价 {analysis.quote.price:.2f}", f"支撑 {analysis.support:.2f}", f"压力 {analysis.resistance:.2f}"],
        actions=[f"回踩 {analysis.support:.2f} 附近观察承接。"],
        invalidations=[f"跌破 {analysis.support:.2f} 先降级。"],
    )
    return analysis, rule_answer


if __name__ == "__main__":
    unittest.main()
