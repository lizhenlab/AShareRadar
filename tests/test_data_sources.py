from __future__ import annotations

import asyncio
from contextlib import redirect_stderr
from datetime import date, datetime, timedelta
import io
import math
import re
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.schemas import Kline, MinuteKline, OrderBook, OrderBookLevel, ProviderCapability, Quote
from app.services import trading_calendar
from app.services.cache import SQLiteCache
from app.services.datahub import DataHub, _provider_error_text as _compat_provider_error_text, _provider_source_key as _compat_provider_source_key
from app.services.datahub_cache import _normalize_minute_interval
from app.services.datahub_klines import KlineCoordinator
from app.services.datahub_metadata import MetadataCoordinator
from app.services.datahub_quotes import QuoteCoordinator
from app.services.datahub_runtime import ProviderRuntime
from app.services.datahub_source_plan import SourcePlanBuilder
from app.services.datahub_status import _provider_error_text, _provider_source_key
from app.services.akshare_provider import (
    ConceptBoardCandidate,
    _concept_constituents_contain,
    _em_concept_candidate,
    _matched_concept_items,
    _ordered_spot_quotes,
    _sina_concept_candidate,
)
from app.services.akshare_mappers import minute_kline_from_hist_row, minute_klines_from_hist_rows, quote_from_spot_row
from app.services.eastmoney_client import (
    eastmoney_get_json,
    eastmoney_kline,
    eastmoney_minute_kline,
    eastmoney_no_proxy,
    eastmoney_quote_from_row,
    eastmoney_quote_params,
    eastmoney_quote_urls,
    eastmoney_quotes,
    eastmoney_history_json,
    merge_no_proxy,
)
from app.services.provider_errors import ProviderCoverageMiss, ProviderError, ProviderProtocolError, ProviderTransportError
from app.services.provider_registry import provider_priority
from app.services.providers import stamp_daily_kline_contract
from app.services.provider_stock_mappers import stock_info_from_baostock_row
from app.services.local_metadata_provider import LocalIndividualStockProvider
from app.services.optional_providers import _import_akshare
from app.config import Settings
from app.utils.errors import NotFoundError
from app.utils.time import now_text
from app.workflows import individual
from tests.factories import (
    make_kline as _kline,
    make_plate_item as _plate_item,
    make_quote as _quote,
    make_stock_info as _stock_info,
)


QUOTE_TEST_NOW = datetime(2026, 5, 13, 10, 5)


def _quote_test_now() -> datetime:
    return QUOTE_TEST_NOW


def _set_quote_test_clock(hub: DataHub) -> None:
    hub._quote_coordinator._now = _quote_test_now

class TradingCalendarTests(unittest.TestCase):
    def test_latest_expected_trade_date_uses_cached_trade_days(self) -> None:
        trade_days = {date(2026, 2, 13), date(2026, 2, 24)}
        with patch("app.services.trading_calendar._trade_days", return_value=trade_days):
            self.assertEqual(trading_calendar.latest_expected_trade_date(datetime(2026, 2, 24, 14, 0, 0)), date(2026, 2, 13))
            self.assertEqual(trading_calendar.expected_quote_date(datetime(2026, 2, 24, 9, 20, 0)), date(2026, 2, 24))
            self.assertEqual(trading_calendar.trading_day_gap(date(2026, 2, 13), date(2026, 2, 24)), 1)

    def test_refresh_trade_calendar_result_reports_fetch_error(self) -> None:
        class FakeAk:
            @staticmethod
            def tool_trade_date_hist_sina():
                raise RuntimeError("calendar source down")

        trading_calendar._trade_days.cache_clear()
        with patch.dict("sys.modules", {"akshare": FakeAk}):
            result = trading_calendar.refresh_trade_calendar_result()

        self.assertFalse(result.ok)
        self.assertEqual(result.trade_date_count, 0)
        self.assertIn("RuntimeError: calendar source down", result.error or "")

    def test_refresh_trade_calendar_result_reports_empty_source(self) -> None:
        class EmptyFrame:
            columns: list[str] = []

        class FakeAk:
            @staticmethod
            def tool_trade_date_hist_sina():
                return EmptyFrame()

        trading_calendar._trade_days.cache_clear()
        with patch.dict("sys.modules", {"akshare": FakeAk}):
            result = trading_calendar.refresh_trade_calendar_result()

        self.assertFalse(result.ok)
        self.assertEqual(result.trade_date_count, 0)
        self.assertEqual(result.error, "AKShare 交易日历返回为空")

class DataSourceReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        quote_clock = patch(
            "app.services.datahub_quotes._quote_now",
            return_value=datetime(2026, 5, 13, 10, 5),
        )
        quote_clock.start()
        self.addCleanup(quote_clock.stop)

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
        self.assertIs(_compat_provider_source_key, _provider_source_key)

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

    def test_consistency_skips_unregistered_priority_provider(self) -> None:
        async def run_check(path: Path) -> tuple[str, list[str], int]:
            hub = DataHub(cache=SQLiteCache(path))
            with patch.object(hub, "_priority", return_value=[(1, "missing")]):
                return await hub._quote_consistency(_quote(source="腾讯行情"))

        with TemporaryDirectory() as tmpdir:
            level, notes, penalty = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

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
            _set_quote_test_clock(hub)
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
            _set_quote_test_clock(hub)
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
        self.assertIs(_compat_provider_error_text, _provider_error_text)

    def test_minute_interval_normalization_accepts_common_aliases(self) -> None:
        self.assertEqual(_normalize_minute_interval("5min"), "5m")
        self.assertEqual(_normalize_minute_interval("1h"), "60m")
        with self.assertRaises(ValueError):
            _normalize_minute_interval("2h")

    def test_akshare_eastmoney_requests_bypass_system_proxy(self) -> None:
        merged = merge_no_proxy("localhost,example.com")
        self.assertIn("localhost", merged)
        self.assertIn("eastmoney.com", merged)
        self.assertIn("push2his.eastmoney.com", merged)

        with patch.dict("os.environ", {"NO_PROXY": "localhost", "no_proxy": "localhost"}, clear=False):
            with eastmoney_no_proxy():
                self.assertIn("eastmoney.com", __import__("os").environ["NO_PROXY"])
                self.assertIn("push2his.eastmoney.com", __import__("os").environ["no_proxy"])
            self.assertEqual(__import__("os").environ["NO_PROXY"], "localhost")

    def test_eastmoney_no_proxy_serializes_environment_changes(self) -> None:
        worker_started = threading.Event()
        worker_entered = threading.Event()

        def worker() -> None:
            worker_started.set()
            with eastmoney_no_proxy():
                worker_entered.set()

        with patch.dict("os.environ", {"NO_PROXY": "localhost", "no_proxy": "localhost"}, clear=False):
            with eastmoney_no_proxy():
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(worker_started.wait(1))
                self.assertFalse(worker_entered.wait(0.05))
            self.assertTrue(worker_entered.wait(1))
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(__import__("os").environ["NO_PROXY"], "localhost")

    def test_market_quote_endpoints_are_https_only(self) -> None:
        self.assertTrue(eastmoney_quote_urls())
        self.assertTrue(all(url.startswith("https://") for url in eastmoney_quote_urls()))

        calls: list[str] = []

        def fake_get_json(url, params):
            calls.append(url)
            return {"rc": 0, "data": {"klines": []}}

        with patch("app.services.eastmoney_client.eastmoney_get_json", side_effect=fake_get_json):
            eastmoney_history_json("600519.SH", period="101", include_market_cap=True)

        self.assertTrue(calls)
        self.assertTrue(all(url.startswith("https://") for url in calls))

    def test_eastmoney_get_json_rejects_plain_http_before_session_open(self) -> None:
        with patch("app.services.eastmoney_client._eastmoney_session") as session:
            with self.assertRaisesRegex(ProviderProtocolError, "仅允许 HTTPS"):
                eastmoney_get_json("http://example.test/quote", {})

        session.assert_not_called()

    def test_eastmoney_light_quote_maps_to_quote_model(self) -> None:
        quote = eastmoney_quote_from_row(
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
                "f86": "20260513100000",
            }
        )

        self.assertEqual(quote.code, "600519")
        self.assertEqual(quote.market, "SH")
        self.assertEqual(quote.name, "贵州茅台")
        self.assertEqual(quote.price, 1303.0)
        self.assertEqual(quote.timestamp, "2026-05-13 10:00:00")
        self.assertEqual(quote.source, "AKShare·东方财富直连")

    def test_eastmoney_quotes_returns_empty_without_network_for_empty_request(self) -> None:
        with patch("app.services.eastmoney_client.eastmoney_get_json") as get_json:
            rows = eastmoney_quotes([])

        self.assertEqual(rows, [])
        get_json.assert_not_called()

    def test_eastmoney_quotes_classifies_empty_successful_response_as_coverage_miss(self) -> None:
        payload = {"rc": 0, "data": {"diff": []}}

        with patch("app.services.eastmoney_client.eastmoney_get_json", return_value=payload):
            with self.assertRaisesRegex(ProviderCoverageMiss, "未覆盖请求股票"):
                eastmoney_quotes(["688001.SH"])

    def test_eastmoney_quotes_classifies_malformed_data_as_protocol_failure(self) -> None:
        payload = {"rc": 0, "data": []}

        with patch("app.services.eastmoney_client.eastmoney_get_json", return_value=payload):
            with self.assertRaisesRegex(ProviderError, "data 字段结构异常") as raised:
                eastmoney_quotes(["600519.SH"])

        self.assertNotIsInstance(raised.exception, ProviderCoverageMiss)

    def test_eastmoney_quotes_keeps_covered_rows_when_batch_is_partially_covered(self) -> None:
        payload = {
            "rc": 0,
            "data": {
                "diff": [
                    {"f2": 1303.0, "f12": "600519", "f14": "贵州茅台", "f18": 1273.38, "f86": "20260513100000"},
                ]
            },
        }

        with patch("app.services.eastmoney_client.eastmoney_get_json", return_value=payload):
            rows = eastmoney_quotes(["600519.SH", "688001.SH"])

        self.assertEqual([f"{item.code}.{item.market}" for item in rows], ["600519.SH"])

    def test_eastmoney_quote_params_dedupes_fetch_symbols_but_keeps_market(self) -> None:
        params = eastmoney_quote_params(["600519.SH", "000001.SZ", "600519.SH", "sz000001"])

        self.assertEqual(params["secids"], "1.600519,0.000001")
        self.assertIn("f115", params["fields"])

    def test_eastmoney_quote_params_rejects_invalid_symbols_with_readable_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "东方财富行情请求包含无效股票代码"):
            eastmoney_quote_params(["600519.SH", "bad-symbol"])

    def test_eastmoney_get_json_rejects_non_object_payloads(self) -> None:
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self):
                return []

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def get(self, *args, **kwargs):
                return FakeResponse()

        with patch("app.services.eastmoney_client._eastmoney_session", return_value=FakeSession()):
            with self.assertRaisesRegex(RuntimeError, "东方财富接口返回结构异常"):
                eastmoney_get_json("https://example.test", {})

    def test_eastmoney_get_json_sanitizes_unexpected_session_errors(self) -> None:
        class FailingSession:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def get(self, *args, **kwargs):
                raise RuntimeError(
                    "GET https://alice:secret@example.test/quote?token=raw-token failed"
                )

        with patch("app.services.eastmoney_client._eastmoney_session", return_value=FailingSession()):
            with self.assertRaises(ProviderTransportError) as raised:
                eastmoney_get_json("https://example.test", {})

        error = str(raised.exception)
        self.assertNotIn("alice", error)
        self.assertNotIn("secret", error)
        self.assertNotIn("raw-token", error)

    def test_eastmoney_quotes_preserves_request_order_after_endpoint_retry(self) -> None:
        payload = {
            "rc": 0,
            "data": {
                "diff": [
                    {"f2": 11.2, "f12": "000001", "f14": "平安银行", "f18": 11.0, "f86": "20260513100000"},
                    {"f2": 1303.0, "f12": "600519", "f14": "贵州茅台", "f18": 1273.38, "f86": "20260513100000"},
                ]
            },
        }

        def fake_get_json(url, params):
            if url.startswith("https://82."):
                raise RuntimeError("primary down")
            return payload

        with patch("app.services.eastmoney_client.eastmoney_get_json", side_effect=fake_get_json):
            rows = eastmoney_quotes(["600519.SH", "000001.SZ", "600519.SH"])

        self.assertEqual([f"{item.code}.{item.market}" for item in rows], ["600519.SH", "000001.SZ", "600519.SH"])
        self.assertTrue(all(item.source == "AKShare·东方财富直连" for item in rows))

    def test_eastmoney_quotes_skips_non_dict_rows_before_ordering(self) -> None:
        payload = {
            "rc": 0,
            "data": {
                "diff": [
                    "bad-row",
                    {"f2": 1303.0, "f12": "600519", "f14": "贵州茅台", "f18": 1273.38, "f86": "20260513100000"},
                ]
            },
        }

        with patch("app.services.eastmoney_client.eastmoney_get_json", return_value=payload):
            rows = eastmoney_quotes(["600519.SH"])

        self.assertEqual([f"{item.code}.{item.market}" for item in rows], ["600519.SH"])

    def test_eastmoney_light_quote_rejects_missing_code(self) -> None:
        with self.assertRaises(ValueError):
            eastmoney_quote_from_row({"f2": 10.0, "f14": "无代码行"})

    def test_eastmoney_light_quote_rejects_short_or_zero_code(self) -> None:
        for raw_code in ("1", "000000"):
            with self.subTest(raw_code=raw_code):
                with self.assertRaises(ValueError):
                    eastmoney_quote_from_row({"f12": raw_code, "f2": 10.0, "f14": "非法代码"})

    def test_eastmoney_light_quote_rejects_invalid_critical_prices(self) -> None:
        base = {"f12": "600519", "f14": "贵州茅台", "f2": 1303.0, "f18": 1273.38}
        invalid_rows = [
            {**base, "f2": "bad"},
            {**base, "f18": 0},
            {**base, "f15": "bad"},
            {**base, "f15": 1260.0, "f16": 1270.0},
        ]

        for row in invalid_rows:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    eastmoney_quote_from_row(row)

    def test_eastmoney_light_quote_uses_explicit_numeric_fallbacks(self) -> None:
        quote = eastmoney_quote_from_row(
            {
                "f2": 1303.0,
                "f3": "-",
                "f4": "-",
                "f8": 0,
                "f9": "-",
                "f12": "600519",
                "f14": "贵州茅台",
                "f15": "-",
                "f16": "-",
                "f17": "-",
                "f18": 1273.38,
                "f115": 14.97,
                "f86": "20260513100000",
            }
        )

        self.assertEqual(quote.open, 1303.0)
        self.assertEqual(quote.high, 1303.0)
        self.assertEqual(quote.low, 1303.0)
        self.assertAlmostEqual(quote.change, 29.62)
        self.assertAlmostEqual(quote.change_pct, (1303.0 - 1273.38) / 1273.38 * 100)
        self.assertIsNone(quote.turnover_rate)
        self.assertEqual(quote.pe, 14.97)

    def test_eastmoney_light_quote_rejects_negative_non_price_fields(self) -> None:
        base = {"f12": "600519", "f14": "贵州茅台", "f2": 1303.0, "f18": 1273.38}
        invalid_rows = [
            {**base, "f5": -1},
            {**base, "f6": -1},
            {**base, "f8": -0.01},
            {**base, "f23": -1},
            {**base, "f20": -1},
        ]

        for row in invalid_rows:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    eastmoney_quote_from_row(row)

    def test_eastmoney_light_quote_allows_negative_pe_for_loss_making_stock(self) -> None:
        quote = eastmoney_quote_from_row(
            {"f12": "000001", "f14": "样本", "f2": 10.0, "f18": 9.5, "f9": -12.3, "f86": "20260513100000"}
        )

        self.assertEqual(quote.pe, -12.3)

    def test_eastmoney_kline_fallback_parses_json_rows(self) -> None:
        payload = {"rc": 0, "data": {"klines": ["2026-05-27,1268.02,1303.00,1319.00,1250.10,82728,10586574902.00,5.41,2.33,29.62,0.66"]}}
        with patch("app.services.eastmoney_client.eastmoney_get_json", return_value=payload):
            rows = eastmoney_kline("600519", period="101", limit=5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].date, "2026-05-27")
        self.assertEqual(rows[0].close, 1303.0)
        self.assertEqual(rows[0].source, "AKShare·东方财富直连")

    def test_eastmoney_kline_fallback_skips_malformed_ohlc_rows(self) -> None:
        payload = {
            "rc": 0,
            "data": {
                "klines": [
                    "2026-05-26,100,101,99,98,1000",
                    "2026-05-26,100,101,102,99,-1",
                    None,
                    "2026-05-27,100,101,102,99,2000",
                ]
            },
        }
        with patch("app.services.eastmoney_client.eastmoney_get_json", return_value=payload):
            rows = eastmoney_kline("600519", period="101", limit=5)

        self.assertEqual([item.date for item in rows], ["2026-05-27"])

    def test_eastmoney_kline_rejects_non_positive_limit_before_http(self) -> None:
        with patch("app.services.eastmoney_client.eastmoney_get_json", side_effect=AssertionError("HTTP should not run")):
            with self.assertRaisesRegex(ValueError, "limit 必须大于 0"):
                eastmoney_kline("600519", period="101", limit=0)
            with self.assertRaisesRegex(ValueError, "limit 必须大于 0"):
                eastmoney_minute_kline("600519", period="5", interval="5m", limit=-1)

    def test_eastmoney_minute_kline_parses_turnover_and_skips_bad_rows(self) -> None:
        payload = {
            "rc": 0,
            "data": {
                "klines": [
                    "2026-05-15 10:00,100,101,99,98,1000,1000000,0,0,0,0.1",
                    "2026-05-15 10:01,100,101,102,99,1000,-1,0,0,0,0.1",
                    "2026-05-15 10:02,100,101,102,99,1000,1000000,0,0,0,-0.1",
                    ["bad-row"],
                    "2026-05-15 10:05,100,101,102,99,2000,2000000,0,0,0,0.2",
                ]
            },
        }
        with patch("app.services.eastmoney_client.eastmoney_get_json", return_value=payload):
            rows = eastmoney_minute_kline("600519", period="5", interval="5m", limit=5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].timestamp, "2026-05-15 10:05")
        self.assertEqual(rows[0].turnover_rate, 0.2)
        self.assertEqual(rows[0].source, "AKShare·东方财富直连")

    def test_akshare_quotes_falls_back_to_original_loader_after_direct_failure(self) -> None:
        provider = __import__("app.services.optional_providers", fromlist=["AKShareProvider"]).AKShareProvider()
        raw_rows = [
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
                "更新时间": "2026-05-13 10:00:00",
            }
        ]

        class FakeSeries:
            def __init__(self, values):
                self.values = values

            def astype(self, _type):
                return FakeSeries([str(value) for value in self.values])

            def isin(self, wanted):
                return [value in wanted for value in self.values]

        class FakeFrame:
            def __init__(self, rows):
                self.rows = rows

            def __getitem__(self, key):
                if isinstance(key, str):
                    return FakeSeries([row[key] for row in self.rows])
                return FakeFrame([row for row, include in zip(self.rows, key) if include])

            def iterrows(self):
                return iter(enumerate(self.rows))

        class FakeAk:
            @staticmethod
            def stock_zh_a_spot_em():
                return FakeFrame(raw_rows)

        with patch("app.services.akshare_provider._eastmoney_quotes", side_effect=RuntimeError("direct failed")), patch(
            "app.services.akshare_provider.is_installed", return_value=True
        ), patch.dict("sys.modules", {"akshare": FakeAk}):
            rows = asyncio.run(provider.quotes(["600519.SH"]))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].price, 1303.0)

    def test_akshare_quotes_prefers_light_eastmoney_bridge(self) -> None:
        provider = __import__("app.services.optional_providers", fromlist=["AKShareProvider"]).AKShareProvider()
        bridge_rows = [_quote(source="AKShare·东方财富直连")]

        with patch("app.services.akshare_provider.is_installed", return_value=True), patch(
            "app.services.akshare_provider._eastmoney_quotes", return_value=bridge_rows
        ), patch("app.services.akshare_provider._import_akshare") as import_ak:
            rows = asyncio.run(provider.quotes(["600519.SH"]))

        self.assertEqual(rows, bridge_rows)
        import_ak.assert_not_called()

    def test_akshare_quotes_skips_malformed_rows_and_preserves_request_order(self) -> None:
        provider = __import__("app.services.optional_providers", fromlist=["AKShareProvider"]).AKShareProvider()
        raw_rows = [
            {"代码": "", "名称": "缺代码", "最新价": 99.0},
            {"代码": "000001", "名称": "平安银行", "最新价": 11.2, "昨收": 11.0, "更新时间": "2026-05-13 10:00:00"},
            {"代码": "600519", "名称": "贵州茅台", "最新价": 1303.0, "昨收": 1273.38, "更新时间": "2026-05-13 10:00:00"},
        ]

        class FakeFrame:
            def __init__(self, rows):
                self.rows = rows

            def iterrows(self):
                return iter(enumerate(self.rows))

        class FakeAk:
            @staticmethod
            def stock_zh_a_spot_em():
                return FakeFrame(raw_rows)

        with patch("app.services.akshare_provider._eastmoney_quotes", side_effect=RuntimeError("direct failed")), patch(
            "app.services.akshare_provider.is_installed", return_value=True
        ), patch.dict("sys.modules", {"akshare": FakeAk}):
            rows = asyncio.run(provider.quotes(["600519.SH", "000001.SZ"]))

        self.assertEqual([item.code for item in rows], ["600519", "000001"])
        self.assertTrue(all(item.code != "000000" for item in rows))

    def test_ordered_spot_quotes_reports_missing_codes_after_row_filtering(self) -> None:
        class FakeFrame:
            def iterrows(self):
                return iter(
                    enumerate(
                        [
                            {
                                "代码": "600519",
                                "名称": "贵州茅台",
                                "最新价": 1303.0,
                                "昨收": 1273.38,
                                "更新时间": "2026-05-13 10:00:00",
                            }
                        ]
                    )
                )

        with self.assertRaisesRegex(RuntimeError, "000001"):
            _ordered_spot_quotes(FakeFrame(), ["600519", "000001"], "AKShare")

    def test_akshare_spot_quotes_reject_missing_event_time(self) -> None:
        class FakeFrame:
            def iterrows(self):
                return iter(enumerate([{"代码": "600519", "名称": "贵州茅台", "最新价": 1303.0, "昨收": 1273.38}]))

        with self.assertRaisesRegex(ProviderProtocolError, "缺少可解析的事件时间"):
            _ordered_spot_quotes(FakeFrame(), ["600519"], "AKShare")

    def test_eastmoney_quote_rejects_missing_or_invalid_event_time(self) -> None:
        base = {"f12": "600519", "f14": "贵州茅台", "f2": 1303.0, "f18": 1273.38}

        for event_time in (None, "", "bad-time", "20260230100000"):
            with self.subTest(event_time=event_time):
                with self.assertRaisesRegex(ProviderProtocolError, "事件时间"):
                    eastmoney_quote_from_row({**base, "f86": event_time})

    def test_akshare_concept_candidates_skip_incomplete_rows_and_map_fields(self) -> None:
        em = _em_concept_candidate(
            {
                "板块名称": "白酒概念",
                "板块代码": "BK0896",
                "涨跌幅": 1.2,
                "成交额": 300000000,
                "换手率": 2.4,
                "领涨股票": "贵州茅台",
                "领涨股票-涨跌幅": 3.1,
            }
        )
        sina = _sina_concept_candidate({"label": "gn_baijiu", "板块": "白酒", "涨跌幅": 0.8, "股票名称": "五粮液", "个股-涨跌幅": 2.6})

        assert em is not None
        assert sina is not None
        self.assertEqual(em.lookup_key, "BK0896")
        self.assertEqual(em.source, "AKShare·东方财富概念")
        self.assertEqual(sina.lookup_key, "gn_baijiu")
        self.assertEqual(sina.source, "AKShare·新浪概念")
        self.assertIsNone(_em_concept_candidate({"板块名称": ""}))
        self.assertIsNone(_sina_concept_candidate({"label": "", "板块": "白酒"}))

    def test_matched_concept_items_skips_loader_errors_and_non_members(self) -> None:
        candidates = [
            None,
            ConceptBoardCandidate("跳过概念", "bad", 0, None, None, None, None, "测试匹配", "测试源"),
            ConceptBoardCandidate("命中概念", "hit", 1.5, 1000000, 0.8, "贵州茅台", 2.5, "测试匹配", "测试源"),
        ]

        def load_constituents(candidate):
            if candidate.lookup_key == "bad":
                raise RuntimeError("remote down")
            return object()

        with patch("app.services.akshare_provider._concept_constituents_contain", return_value=True):
            items = _matched_concept_items(candidates, load_constituents, "600519.SH", "600519", "2026-05-15 10:00:00", 3)

        self.assertEqual([item.name for item in items], ["命中概念"])
        self.assertEqual(items[0].rank, 1)
        self.assertEqual(items[0].leading_stock, "贵州茅台")

    def test_matched_concept_items_raises_when_all_constituent_loads_fail(self) -> None:
        candidates = [
            ConceptBoardCandidate("概念A", "a", 0, None, None, None, None, "测试匹配", "测试源"),
            ConceptBoardCandidate("概念B", "b", 0, None, None, None, None, "测试匹配", "测试源"),
        ]

        def load_constituents(_candidate):
            raise RuntimeError("remote schema changed")

        with self.assertRaisesRegex(RuntimeError, "概念成分源不可用"):
            _matched_concept_items(candidates, load_constituents, "600519.SH", "600519", "2026-05-15 10:00:00", 3)

    def test_concept_constituents_match_common_code_columns(self) -> None:
        class FakeSeries:
            def __init__(self, values):
                self.values = values

            def astype(self, _type):
                return self

            @property
            def str(self):
                return self

            def extract(self, _pattern, expand=False):
                return FakeSeries([match.group(1) for value in self.values if (match := re.search(r"(\d{6})", value))])

            def dropna(self):
                return self.values

        class FakeFrame:
            empty = False
            columns = ["股票简称", "SYMBOL_CODE"]

            def __getitem__(self, column):
                return FakeSeries(["贵州茅台", "sh600519"] if column == "SYMBOL_CODE" else ["贵州茅台"])

        self.assertTrue(_concept_constituents_contain(FakeFrame(), "600519"))

    def test_akshare_minute_kline_maps_interval_to_eastmoney_period(self) -> None:
        provider = __import__("app.services.optional_providers", fromlist=["AKShareProvider"]).AKShareProvider()
        calls = {}

        class FakeFrame:
            def __init__(self, rows):
                self.rows = rows

            def tail(self, limit):
                return FakeFrame(self.rows[-limit:])

            def iterrows(self):
                return iter(enumerate(self.rows))

        class FakeAk:
            @staticmethod
            def stock_zh_a_hist_min_em(symbol, period, adjust):
                calls.update({"symbol": symbol, "period": period, "adjust": adjust})
                return FakeFrame(
                    [
                        {
                            "时间": "2026-05-15 10:00:00",
                            "开盘": 100,
                            "收盘": 101,
                            "最高": 102,
                            "最低": 99,
                            "成交量": 1234,
                            "成交额": 12340000,
                            "换手率": 0.3,
                        }
                    ]
                )

        with patch("app.services.akshare_provider.is_installed", return_value=True), patch.dict("sys.modules", {"akshare": FakeAk}):
            rows = asyncio.run(provider.minute_kline("600519.SH", interval="15m", limit=1))

        self.assertEqual(calls, {"symbol": "600519", "period": "15", "adjust": "qfq"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].timestamp, "2026-05-15 10:00:00")
        self.assertEqual(rows[0].interval, "15m")
        self.assertEqual(rows[0].source, "AKShare")

    def test_akshare_minute_mapper_filters_invalid_rows_and_normalizes_optional_fields(self) -> None:
        rows = minute_klines_from_hist_rows(
            [
                {
                    "时间": "",
                    "开盘": 100,
                    "收盘": 101,
                    "最高": 102,
                    "最低": 99,
                    "成交量": 1234,
                },
                {
                    "时间": "2026-05-15 10:00:00",
                    "开盘": 100,
                    "收盘": 0,
                    "最高": 102,
                    "最低": 99,
                    "成交量": 1234,
                },
                {
                    "时间": "2026-05-15 10:03:00",
                    "开盘": 100,
                    "收盘": 101,
                    "最高": 100.5,
                    "最低": 99,
                    "成交量": 1234,
                },
                {
                    "时间": "2026-05-15 10:05:00",
                    "开盘": 100,
                    "收盘": 101,
                    "最高": 102,
                    "最低": 99,
                    "成交量": 1234,
                    "成交额": 0,
                    "换手率": 0,
                },
            ],
            interval="5m",
            source_name="AKShare",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].timestamp, "2026-05-15 10:05:00")
        self.assertIsNone(rows[0].amount)
        self.assertIsNone(rows[0].turnover_rate)
        self.assertIsNone(minute_kline_from_hist_row({"时间": "2026-05-15 10:10:00", "收盘": -1}, interval="5m", source_name="AKShare"))

    def test_akshare_quote_mapper_requires_positive_price_and_computes_missing_change_pct(self) -> None:
        row = {
            "代码": "600519",
            "名称": "贵州茅台",
            "最新价": 110,
            "昨收": 100,
            "今开": 0,
            "最高": "",
            "最低": None,
            "成交量": 123,
            "成交额": 456,
        }

        quote = quote_from_spot_row(row, stamp="2026-05-13 10:00:00", source_name="AKShare")

        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.code, "600519")
        self.assertEqual(quote.market, "SH")
        self.assertEqual(quote.open, 110)
        self.assertEqual(quote.high, 110)
        self.assertEqual(quote.low, 110)
        self.assertEqual(quote.change_pct, 10)
        self.assertIsNone(quote_from_spot_row({**row, "最新价": 0}, stamp="2026-05-13 10:00:00", source_name="AKShare"))

    def test_baostock_mapper_normalizes_market_and_compact_ipo_date(self) -> None:
        item = stock_info_from_baostock_row(
            {"code": "sz.000001", "code_name": "平安银行", "ipoDate": "19910403"},
            stamp="2026-05-13 10:00:00",
            source_name="BaoStock",
        )

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.symbol, "000001.SZ")
        self.assertEqual(item.market, "SZ")
        self.assertEqual(item.list_date, "1991-04-03")
        self.assertIsNone(
            stock_info_from_baostock_row({"code": "bj.430001", "code_name": "北交所"}, stamp="2026-05-13 10:00:00", source_name="BaoStock")
        )

    def test_akshare_minute_kline_uses_light_fallback_when_import_fails(self) -> None:
        provider = __import__("app.services.optional_providers", fromlist=["AKShareProvider"]).AKShareProvider()
        fallback_rows = [
            MinuteKline(
                timestamp="2026-05-15 10:00:00",
                open=100,
                close=101,
                high=102,
                low=99,
                volume=1234,
                amount=12340000,
                interval="5m",
                source="东方财富直连",
            )
        ]

        with patch("app.services.akshare_provider.is_installed", return_value=True), patch(
            "app.services.akshare_provider._import_akshare", side_effect=RuntimeError("AKShare 依赖不可用")
        ), patch("app.services.akshare_provider._eastmoney_minute_kline", return_value=fallback_rows) as fallback:
            rows = asyncio.run(provider.minute_kline("600519.SH", interval="5m", limit=1))

        fallback.assert_called_once_with("600519.SH", period="5", interval="5m", limit=1)
        self.assertEqual(rows, fallback_rows)

    def test_akshare_daily_kline_uses_light_fallback_when_import_fails(self) -> None:
        provider = __import__("app.services.optional_providers", fromlist=["AKShareProvider"]).AKShareProvider()
        fallback_rows = [Kline(date="2026-05-15", open=100, close=101, high=102, low=99, volume=1234)]

        with patch("app.services.akshare_provider.is_installed", return_value=True), patch(
            "app.services.akshare_provider._import_akshare", side_effect=RuntimeError("AKShare 依赖不可用")
        ), patch("app.services.akshare_provider._eastmoney_kline", return_value=fallback_rows) as fallback:
            rows = asyncio.run(provider.kline("600519.SH", limit=1))

        fallback.assert_called_once_with("600519.SH", period="101", limit=1)
        self.assertEqual(
            rows,
            stamp_daily_kline_contract(
                fallback_rows,
                adjustment_mode="qfq",
                source="AKShare",
            ),
        )

    def test_akshare_daily_kline_schema_error_does_not_use_light_fallback(self) -> None:
        provider = __import__("app.services.optional_providers", fromlist=["AKShareProvider"]).AKShareProvider()

        class FakeFrame:
            def tail(self, _limit):
                return self

            def iterrows(self):
                return iter(enumerate([{"开盘": 100, "收盘": 101, "最高": 102, "最低": 99, "成交量": 1234}]))

        class FakeAk:
            @staticmethod
            def stock_zh_a_hist(symbol, period, adjust):
                return FakeFrame()

        with patch("app.services.akshare_provider.is_installed", return_value=True), patch.dict(
            "sys.modules", {"akshare": FakeAk}
        ), patch("app.services.akshare_provider._eastmoney_kline") as fallback:
            with self.assertRaisesRegex(RuntimeError, "AKShare日K字段缺失"):
                asyncio.run(provider.kline("600519.SH", limit=1))

        fallback.assert_not_called()

    def test_akshare_minute_schema_error_does_not_use_light_fallback(self) -> None:
        provider = __import__("app.services.optional_providers", fromlist=["AKShareProvider"]).AKShareProvider()

        class FakeFrame:
            def tail(self, _limit):
                return self

            def iterrows(self):
                return iter(enumerate([{"bad": 1}]))

        class FakeAk:
            @staticmethod
            def stock_zh_a_hist_min_em(symbol, period, adjust):
                return FakeFrame()

        with patch("app.services.akshare_provider.is_installed", return_value=True), patch.dict(
            "sys.modules", {"akshare": FakeAk}
        ), patch("app.services.akshare_provider._eastmoney_minute_kline") as fallback:
            rows = asyncio.run(provider.minute_kline("600519.SH", interval="5m", limit=1))

        self.assertEqual(rows, [])
        fallback.assert_not_called()

    def test_akshare_import_failure_is_compact_and_quiet(self) -> None:
        def noisy_import(name: str):
            self.assertEqual(name, "akshare")
            print("native import traceback noise", file=sys.stderr)
            raise ImportError("numpy ABI mismatch")

        stderr = io.StringIO()
        with patch("app.services.akshare_provider.importlib.import_module", side_effect=noisy_import):
            with redirect_stderr(stderr):
                with self.assertRaisesRegex(RuntimeError, "AKShare 依赖不可用"):
                    _import_akshare()

        self.assertEqual(stderr.getvalue(), "")

    def test_akshare_stock_pool_skips_rows_without_valid_code(self) -> None:
        provider = __import__("app.services.optional_providers", fromlist=["AKShareProvider"]).AKShareProvider()
        raw_rows = [
            {"code": "", "name": "缺代码"},
            {"code": "000000", "name": "非法零代码"},
            {"code": "600519", "name": "贵州茅台"},
        ]

        class FakeFrame:
            def __init__(self, rows):
                self.rows = rows

            def iterrows(self):
                return iter(enumerate(self.rows))

        class FakeAk:
            @staticmethod
            def stock_info_a_code_name():
                return FakeFrame(raw_rows)

        with patch("app.services.akshare_provider.is_installed", return_value=True), patch.dict(
            "sys.modules",
            {"akshare": FakeAk},
        ):
            rows = asyncio.run(provider.stock_pool())

        self.assertEqual([item.code for item in rows], ["600519"])

    def test_tushare_stock_pool_skips_rows_without_valid_code(self) -> None:
        provider = __import__("app.services.optional_providers", fromlist=["TushareProvider"]).TushareProvider(token="test-token")
        raw_rows = [
            {"ts_code": "", "name": "缺代码", "industry": "未知", "list_date": ""},
            {"ts_code": "000000.SZ", "name": "非法零代码", "industry": "未知", "list_date": "20200101"},
            {"ts_code": "600519.SH", "name": "贵州茅台", "industry": "白酒", "list_date": "20010827"},
        ]

        class FakeFrame:
            def __init__(self, rows):
                self.rows = rows

            def iterrows(self):
                return iter(enumerate(self.rows))

        class FakePro:
            def stock_basic(self, exchange: str = "", list_status: str = "L", fields: str = ""):
                return FakeFrame(raw_rows)

        class FakeTs:
            @staticmethod
            def set_token(token: str) -> None:
                return None

            @staticmethod
            def pro_api(token: str):
                if token != "test-token":
                    raise AssertionError(f"unexpected token: {token}")
                return FakePro()

        with patch("app.services.tushare_provider.is_installed", return_value=True), patch.dict("sys.modules", {"tushare": FakeTs}):
            rows = asyncio.run(provider.stock_pool())

        self.assertEqual([item.code for item in rows], ["600519"])
        self.assertEqual(rows[0].list_date, "2001-08-27")

    def test_baostock_stock_pool_skips_rows_without_valid_code(self) -> None:
        provider = __import__("app.services.optional_providers", fromlist=["BaoStockProvider"]).BaoStockProvider()
        raw_rows = [
            ["bj.430001", "北交所样本", "2020-01-01"],
            ["sh.000000", "非法零代码", "2020-01-01"],
            ["sh.600519", "贵州茅台", "2001-08-27"],
        ]

        class FakeLogin:
            error_code = "0"
            error_msg = ""

        class FakeResult:
            error_code = "0"
            error_msg = ""
            fields = ["code", "code_name", "ipoDate"]

            def __init__(self, rows):
                self.rows = rows
                self.index = -1

            def next(self):
                self.index += 1
                return self.index < len(self.rows)

            def get_row_data(self):
                return self.rows[self.index]

        class FakeBs:
            @staticmethod
            def login():
                return FakeLogin()

            @staticmethod
            def query_stock_basic():
                return FakeResult(raw_rows)

            @staticmethod
            def logout() -> None:
                return None

        with patch("app.services.baostock_provider.is_installed", return_value=True), patch.dict(
            "sys.modules",
            {"baostock": FakeBs},
        ):
            rows = asyncio.run(provider.stock_pool())

        self.assertEqual([item.code for item in rows], ["600519"])
        self.assertEqual(rows[0].market, "SH")

    def test_local_stock_pool_includes_manual_smoke_symbol(self) -> None:
        provider = LocalIndividualStockProvider()

        rows = asyncio.run(provider.stock_pool())
        concepts = asyncio.run(provider.stock_concepts("002182.SZ"))

        target = next((item for item in rows if item.symbol == "002182.SZ"), None)
        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.name, "宝武镁业")
        self.assertEqual(target.industry, "小金属")
        self.assertTrue(any(item.name == "镁金属" for item in concepts))

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
            hub.cache.update_provider_capability_success("akshare", "quote", 2, 12.0)
            with patch.object(hub, "_priority", return_value=[(2, "akshare"), (5, "local")]):
                rows = await hub.plate_rank(limit=1, refresh=True)
            status = next(item for item in hub.cache.provider_statuses() if item.name == "akshare")
            plate_status = next(item for item in hub.cache.provider_capability_statuses() if item.name == "akshare" and item.kind == "plate")
            return rows, status, plate_status

        with TemporaryDirectory() as tmpdir:
            rows, status, plate_status = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(len(rows), 1)
        self.assertTrue(status.healthy)
        self.assertFalse(plate_status.healthy)
        self.assertEqual(plate_status.failure_count, 1)

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

    def test_source_plan_builder_prefers_capability_health_over_provider_health(self) -> None:
        with TemporaryDirectory() as tmpdir:
            hub = DataHub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3"))
            hub.cache.update_provider_success("tencent", 1, 12.0)
            hub.cache.update_provider_capability_failure("tencent", "quote", 1, "quote down")
            builder = SourcePlanBuilder(
                provider_names=lambda: ["tencent"],
                priority=lambda kind: [(1, "tencent")] if kind == "quote" else [],
                provider_index=lambda name: 1,
                is_cooling=lambda name, kind: False,
            )
            plan = builder.build(
                hub.cache.provider_statuses(),
                hub.capabilities(),
                hub.cache.provider_capability_statuses(),
            )

        self.assertIsNone(plan.primary_quote_source)
        decision = next(item for item in plan.decisions if item.name == "tencent")
        self.assertIn("报价最近失败", decision.state)

    def test_source_plan_builder_describes_partial_provider_availability(self) -> None:
        with TemporaryDirectory() as tmpdir:
            hub = DataHub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3"))
            hub.cache.update_provider_capability_success("akshare", "quote", 2, 12.0)
            hub.cache.update_provider_capability_failure("akshare", "kline", 2, "kline down")
            builder = SourcePlanBuilder(
                provider_names=lambda: ["akshare"],
                priority=lambda kind: [(2, "akshare")] if kind in {"quote", "kline"} else [],
                provider_index=lambda name: 2,
                is_cooling=lambda name, kind: False,
            )
            plan = builder.build(
                hub.cache.provider_statuses(),
                hub.capabilities(),
                hub.cache.provider_capability_statuses(),
            )

        decision = next(item for item in plan.decisions if item.name == "akshare")
        self.assertIn("报价正常", decision.state)
        self.assertIn("日K最近失败", decision.state)
        self.assertEqual(decision.action, "仅继续使用正常能力；最近失败的能力会由其他源或缓存兜底。")

    def test_provider_ensure_refreshes_updated_at_when_config_changes(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            with patch(
                "app.repositories.provider_status.now_text",
                side_effect=["2026-05-13 09:30:00", "2026-05-13 09:31:00"],
            ):
                cache.ensure_provider("akshare", 2, enabled=True)
                cache.ensure_provider("akshare", 1, enabled=False)
            status = next(item for item in cache.provider_statuses() if item.name == "akshare")

        self.assertFalse(status.enabled)
        self.assertEqual(status.priority, 1)
        self.assertEqual(status.updated_at, "2026-05-13 09:31:00")

    def test_provider_capability_ensure_refreshes_updated_at_when_config_changes(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            with patch(
                "app.repositories.provider_status.now_text",
                side_effect=["2026-05-13 09:30:00", "2026-05-13 09:31:00"],
            ):
                cache.ensure_provider_capability("akshare", "quote", 2, enabled=True)
                cache.ensure_provider_capability("akshare", "quote", 1, enabled=False)
            status = next(item for item in cache.provider_capability_statuses() if item.name == "akshare" and item.kind == "quote")

        self.assertFalse(status.enabled)
        self.assertEqual(status.priority, 1)
        self.assertEqual(status.updated_at, "2026-05-13 09:31:00")

    def test_provider_capability_runtime_updates_preserve_disabled_config(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            cache.ensure_provider_capability("akshare", "quote", 2, enabled=False)

            cache.update_provider_capability_success("akshare", "quote", 2, 12.0)
            cache.update_provider_capability_failure("akshare", "quote", 2, "quote down")
            capability = next(item for item in cache.provider_capability_statuses() if item.name == "akshare" and item.kind == "quote")
            provider = next(item for item in cache.provider_statuses() if item.name == "akshare")

        self.assertFalse(capability.enabled)
        self.assertFalse(provider.enabled)
        self.assertEqual(capability.success_count, 1)
        self.assertEqual(capability.failure_count, 1)

    def test_provider_runtime_records_capability_state_independently(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            runtime = ProviderRuntime(cache, Settings(provider_failure_cooldown_seconds=30))
            runtime.record_success("tencent", 1, 12.0, "quote")
            runtime.record_failure("tencent", 1, RuntimeError("kline down"), "kline")
            statuses = {(item.name, item.kind): item for item in cache.provider_capability_statuses()}

        self.assertTrue(statuses[("tencent", "quote")].healthy)
        self.assertFalse(statuses[("tencent", "kline")].healthy)
        self.assertFalse(runtime.is_cooling("tencent", "quote"))
        self.assertTrue(runtime.is_cooling("tencent", "kline"))
        runtime.clear_cooldown("tencent", "kline")
        self.assertFalse(runtime.is_cooling("tencent", "kline"))

    def test_disabled_futu_order_book_does_not_record_provider_failure(self) -> None:
        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            try:
                await hub.order_book("600519")
            except RuntimeError as exc:
                order_error = str(exc)
            else:
                order_error = ""
            ping = await hub.futu_ping()
            status = hub.status()
            capability = next((item for item in status.capability_statuses if item.name == "futu" and item.kind == "order_book"), None)
            return order_error, ping, capability

        with TemporaryDirectory() as tmpdir:
            order_error, ping, capability = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertIn("Futu OpenAPI 未启用", order_error)
        self.assertFalse(ping["ok"])
        self.assertIn("Futu OpenAPI 未启用", str(ping["message"]))
        if capability is not None:
            self.assertFalse(capability.enabled)
            self.assertEqual(capability.failure_count, 0)
            self.assertIsNone(capability.last_error)

    def test_futu_quotes_skip_non_a_share_rows_and_preserve_request_order(self) -> None:
        provider = __import__("app.services.optional_providers", fromlist=["FutuProvider"]).FutuProvider(enabled=True)

        class FakeFrame:
            def __init__(self, rows):
                self.rows = rows

            def iterrows(self):
                return iter(enumerate(self.rows))

        class FakeContext:
            def __init__(self, host: str, port: int) -> None:
                self.host = host
                self.port = port

            def get_market_snapshot(self, symbols):
                self.symbols = symbols
                return 0, FakeFrame(
                    [
                        {"code": "HK.00700", "stock_name": "腾讯控股", "last_price": 400.0},
                        {
                            "code": "SZ.000001",
                            "stock_name": "平安银行",
                            "last_price": 11.2,
                            "prev_close_price": 11.0,
                            "update_time": "2026-05-13 10:00:00",
                        },
                        {
                            "code": "SH.600519",
                            "stock_name": "贵州茅台",
                            "last_price": 1303.0,
                            "prev_close_price": 1273.38,
                            "update_time": "2026-05-13 10:00:00",
                        },
                    ]
                )

            def close(self) -> None:
                return None

        fake_futu = __import__("types").SimpleNamespace(OpenQuoteContext=FakeContext, RET_OK=0)
        with patch("app.services.futu_provider.is_installed", return_value=True), patch.dict("sys.modules", {"futu": fake_futu}):
            rows = asyncio.run(provider.quotes(["600519.SH", "000001.SZ"]))

        self.assertEqual([item.code for item in rows], ["600519", "000001"])
        self.assertTrue(all(item.market in {"SH", "SZ"} for item in rows))

    def test_order_book_coordinator_records_successful_futu_capability(self) -> None:
        class EnabledFutuProvider:
            source_name = "Futu OpenAPI"

            def capability(self):
                return ProviderCapability(
                    name="futu",
                    installed=True,
                    enabled=True,
                    reliability_level="授权源",
                    realtime_quote=True,
                    minute_kline=True,
                    order_book=True,
                    note="测试 Futu 盘口源",
                )

            async def order_book(self, symbol: str) -> OrderBook:
                return OrderBook(
                    symbol="600519.SH",
                    code="600519",
                    market="SH",
                    bid=[OrderBookLevel(price=100.0, volume=10.0)],
                    ask=[OrderBookLevel(price=100.1, volume=8.0)],
                    source=self.source_name,
                    updated_at=now_text(),
                )

            async def ping(self) -> str:
                return "Futu OpenD OK"

        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            hub.providers["futu"] = EnabledFutuProvider()
            order_book = await hub.order_book("600519")
            ping = await hub.futu_ping()
            capability = next(
                item
                for item in hub.cache.provider_capability_statuses()
                if item.name == "futu" and item.kind == "order_book"
            )
            return order_book, ping, capability

        with TemporaryDirectory() as tmpdir:
            order_book, ping, capability = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(order_book.source, "Futu OpenAPI")
        self.assertTrue(ping["ok"])
        self.assertTrue(capability.enabled)
        self.assertTrue(capability.healthy)
        self.assertGreaterEqual(capability.success_count, 2)

    def test_quote_coordinator_falls_back_after_primary_failure(self) -> None:
        class FailingProvider:
            source_name = "失败主源"

            async def quotes(self, symbols):
                raise RuntimeError("primary down")

        class BackupProvider:
            source_name = "备用报价源"

            async def quotes(self, symbols):
                return [
                    _quote(source=self.source_name).model_copy(update={"code": symbol.split(".")[0], "market": symbol.split(".")[1]})
                    for symbol in symbols
                ]

        async def run_check(path: Path) -> tuple[list[Quote], bool, int]:
            settings = Settings(provider_failure_cooldown_seconds=60)
            cache = SQLiteCache(path)
            runtime = ProviderRuntime(cache, settings)
            coordinator = QuoteCoordinator(
                settings=settings,
                cache=cache,
                providers={"primary": FailingProvider(), "backup": BackupProvider()},
                runtime=runtime,
                priority=lambda kind: [(1, "primary"), (2, "backup")],
                now=_quote_test_now,
            )
            rows = await coordinator.quotes(["600519.SH"], use_cache=False)
            failure_count = next(item.failure_count for item in cache.provider_statuses() if item.name == "primary")
            return rows, runtime.is_cooling("primary", "quote"), failure_count

        with TemporaryDirectory() as tmpdir:
            rows, cooling, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(rows[0].source, "备用报价源")
        self.assertTrue(cooling)
        self.assertEqual(failure_count, 1)

    def test_quote_coordinator_rejects_invalid_primary_quote_before_backup(self) -> None:
        class InvalidProvider:
            source_name = "坏报价源"

            async def quotes(self, symbols):
                return [
                    _quote(source=self.source_name).model_copy(
                        update={"code": symbol.split(".")[0], "market": symbol.split(".")[1], "price": math.inf}
                    )
                    for symbol in symbols
                ]

        class BackupProvider:
            source_name = "备用报价源"

            async def quotes(self, symbols):
                return [
                    _quote(source=self.source_name).model_copy(update={"code": symbol.split(".")[0], "market": symbol.split(".")[1]})
                    for symbol in symbols
                ]

        async def run_check(path: Path) -> tuple[list[Quote], bool, str | None]:
            settings = Settings(provider_failure_cooldown_seconds=60)
            cache = SQLiteCache(path)
            runtime = ProviderRuntime(cache, settings)
            coordinator = QuoteCoordinator(
                settings=settings,
                cache=cache,
                providers={"invalid": InvalidProvider(), "backup": BackupProvider()},
                runtime=runtime,
                priority=lambda kind: [(1, "invalid"), (2, "backup")],
                now=_quote_test_now,
            )
            rows = await coordinator.quotes(["600519.SH"], use_cache=False)
            status = next(item for item in cache.provider_capability_statuses() if item.name == "invalid" and item.kind == "quote")
            return rows, runtime.is_cooling("invalid", "quote"), status.last_error

        with TemporaryDirectory() as tmpdir:
            rows, cooling, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(rows[0].source, "备用报价源")
        self.assertTrue(cooling)
        self.assertEqual(last_error, "坏报价源 行情缺失或字段无效：600519.SH")

    def test_quote_coordinator_partial_success_keeps_provider_healthy_and_fetches_only_missing(self) -> None:
        class PartialProvider:
            source_name = "部分报价源"

            async def quotes(self, symbols):
                return [
                    _quote(source=self.source_name).model_copy(update={"code": symbols[0].split(".")[0], "market": symbols[0].split(".")[1]})
                ]

        class BackupProvider:
            source_name = "备用报价源"

            def __init__(self) -> None:
                self.requested: list[str] = []

            async def quotes(self, symbols):
                self.requested.extend(symbols)
                return [
                    _quote(source=self.source_name).model_copy(update={"code": symbol.split(".")[0], "market": symbol.split(".")[1]})
                    for symbol in symbols
                ]

        async def run_check(path: Path) -> tuple[list[Quote], list[str], bool, bool, int, int, str | None]:
            settings = Settings(provider_failure_cooldown_seconds=60)
            cache = SQLiteCache(path)
            backup = BackupProvider()
            runtime = ProviderRuntime(cache, settings)
            coordinator = QuoteCoordinator(
                settings=settings,
                cache=cache,
                providers={"partial": PartialProvider(), "backup": backup},
                runtime=runtime,
                priority=lambda kind: [(1, "partial"), (2, "backup")],
                now=_quote_test_now,
            )
            rows = await coordinator.quotes(["600519.SH", "000001.SZ"], use_cache=False)
            status = next(item for item in cache.provider_capability_statuses() if item.name == "partial" and item.kind == "quote")
            return rows, backup.requested, runtime.is_cooling("partial", "quote"), status.healthy, status.success_count, status.failure_count, status.last_error

        with TemporaryDirectory() as tmpdir:
            rows, backup_requested, cooling, healthy, success_count, failure_count, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual([f"{item.code}.{item.market}" for item in rows], ["600519.SH", "000001.SZ"])
        self.assertEqual([item.source for item in rows], ["部分报价源", "备用报价源"])
        self.assertEqual(backup_requested, ["000001.SZ"])
        self.assertFalse(cooling)
        self.assertTrue(healthy)
        self.assertEqual(success_count, 1)
        self.assertEqual(failure_count, 0)
        self.assertIsNone(last_error)

    def test_quote_coordinator_returns_provider_rows_when_cache_write_fails(self) -> None:
        class CacheWriteFailingSQLiteCache(SQLiteCache):
            def save_quotes(self, quotes: list[Quote]) -> None:
                raise sqlite3.DatabaseError("quote cache readonly")

        class LiveProvider:
            source_name = "实时报价源"

            async def quotes(self, symbols):
                return [
                    _quote(source=self.source_name).model_copy(update={"code": symbol.split(".")[0], "market": symbol.split(".")[1]})
                    for symbol in symbols
                ]

        async def run_check(path: Path) -> tuple[list[Quote], bool, int]:
            settings = Settings(provider_failure_cooldown_seconds=60)
            cache = CacheWriteFailingSQLiteCache(path)
            runtime = ProviderRuntime(cache, settings)
            coordinator = QuoteCoordinator(
                settings=settings,
                cache=cache,
                providers={"live": LiveProvider()},
                runtime=runtime,
                priority=lambda kind: [(1, "live")],
                now=_quote_test_now,
            )
            rows = await coordinator.quotes(["600519.SH"], use_cache=False)
            status = next(item for item in cache.provider_capability_statuses() if item.name == "live" and item.kind == "quote")
            return rows, runtime.is_cooling("live", "quote"), status.failure_count

        with TemporaryDirectory() as tmpdir:
            rows, cooling, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(rows[0].source, "实时报价源")
        self.assertFalse(cooling)
        self.assertEqual(failure_count, 0)

    def test_quote_coordinator_skips_unregistered_priority_provider(self) -> None:
        class BackupProvider:
            source_name = "备用报价源"

            async def quotes(self, symbols):
                return [
                    _quote(source=self.source_name).model_copy(update={"code": symbol.split(".")[0], "market": symbol.split(".")[1]})
                    for symbol in symbols
                ]

        async def run_check(path: Path) -> list[Quote]:
            settings = Settings()
            cache = SQLiteCache(path)
            coordinator = QuoteCoordinator(
                settings=settings,
                cache=cache,
                providers={"backup": BackupProvider()},
                runtime=ProviderRuntime(cache, settings),
                priority=lambda kind: [(1, "missing"), (2, "backup")],
                now=_quote_test_now,
            )
            return await coordinator.quotes(["600519.SH"], use_cache=False)

        with TemporaryDirectory() as tmpdir:
            rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(rows[0].source, "备用报价源")

    def test_quote_coordinator_dedupes_fetch_but_preserves_requested_order(self) -> None:
        class RecordingProvider:
            source_name = "顺序测试源"

            def __init__(self) -> None:
                self.requested: list[str] = []

            async def quotes(self, symbols):
                self.requested.extend(symbols)
                return [
                    _quote(source=self.source_name).model_copy(update={"code": symbol.split(".")[0], "market": symbol.split(".")[1]})
                    for symbol in symbols
                ]

        async def run_check(path: Path) -> tuple[list[Quote], list[str]]:
            settings = Settings()
            cache = SQLiteCache(path)
            provider = RecordingProvider()
            runtime = ProviderRuntime(cache, settings)
            coordinator = QuoteCoordinator(
                settings=settings,
                cache=cache,
                providers={"recording": provider},
                runtime=runtime,
                priority=lambda kind: [(1, "recording")],
                now=_quote_test_now,
            )
            rows = await coordinator.quotes(["600519.SH", "000001.SZ", "600519.SH"], use_cache=False)
            return rows, provider.requested

        with TemporaryDirectory() as tmpdir:
            rows, requested = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(requested, ["600519.SH", "000001.SZ"])
        self.assertEqual([f"{item.code}.{item.market}" for item in rows], ["600519.SH", "000001.SZ", "600519.SH"])

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

        async def run_check(path: Path) -> tuple[str, str, bool, bool]:
            hub = DataHub(cache=SQLiteCache(path))
            _set_quote_test_clock(hub)
            hub.cache.save_quotes([_quote(source="腾讯行情")])
            hub.providers["backup"] = BackupProvider()
            quote = (await hub.quotes(["600519.SH"]))[0]
            with patch.object(hub, "_priority", return_value=[(1, "tencent"), (2, "backup")]):
                level, _, _ = await hub._quote_consistency(quote)
            return quote.source, level, quote.from_cache, quote.fallback_used

        with TemporaryDirectory() as tmpdir:
            source, level, from_cache, fallback_used = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertIn("短时缓存", source)
        self.assertEqual(level, "一致")
        self.assertTrue(from_cache)
        self.assertFalse(fallback_used)

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
            _set_quote_test_clock(hub)
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
        self.assertTrue(rows[0].from_cache)
        self.assertFalse(rows[0].fallback_used)
        self.assertEqual(rows[1].source, "实时补齐源")
        self.assertFalse(rows[1].from_cache)
        self.assertFalse(rows[1].fallback_used)

    def test_quotes_mark_fallback_cache_with_machine_readable_flags(self) -> None:
        class FailingQuoteProvider:
            source_name = "失败行情源"

            async def quotes(self, symbols):
                raise RuntimeError("quote down")

        async def run_check(path: Path):
            settings = Settings(cache_path=path, provider_failure_cooldown_seconds=60)
            cache = SQLiteCache(path)
            cache.save_quotes([_quote(source="腾讯行情")])
            hub = DataHub(cache=cache, settings=settings)
            _set_quote_test_clock(hub)
            hub.providers["failing"] = FailingQuoteProvider()
            with patch.object(hub, "_priority", return_value=[(1, "failing")]):
                return await hub.quotes(["600519.SH"], use_cache=False)

        with TemporaryDirectory() as tmpdir:
            rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(len(rows), 1)
        self.assertIn("兜底缓存", rows[0].source)
        self.assertTrue(rows[0].from_cache)
        self.assertTrue(rows[0].fallback_used)

    def test_quote_with_quality_use_cache_false_fetches_live_quote_and_still_checks_consistency(self) -> None:
        class LiveProvider:
            source_name = "实时测试源"

            async def quotes(self, symbols):
                return [_quote(source=self.source_name).model_copy(update={"code": symbol.split(".")[0], "market": symbol.split(".")[1]}) for symbol in symbols]

        async def run_check(path: Path) -> tuple[str, str]:
            hub = DataHub(cache=SQLiteCache(path))
            _set_quote_test_clock(hub)
            hub.cache.save_quotes([_quote(source="腾讯行情")])
            hub.providers["live"] = LiveProvider()
            with patch.object(hub, "_priority", return_value=[(1, "live")]):
                quote, quality = await hub.quote_with_quality("600519.SH", use_cache=False)
            return quote.source, quality.consistency_level

        with TemporaryDirectory() as tmpdir:
            source, consistency_level = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(source, "实时测试源")
        self.assertNotEqual(consistency_level, "未校验")

    def test_kline_coordinator_uses_fallback_cache_after_provider_failure(self) -> None:
        class FailingKlineProvider:
            source_name = "失败K线源"

            async def kline(self, symbol, limit: int = 120):
                raise RuntimeError("kline down")

        async def run_check(path: Path):
            settings = Settings(provider_failure_cooldown_seconds=60)
            cache = SQLiteCache(path)
            cache.save_klines(
                "600519.SH",
                [_kline(date=f"2026-05-{index + 1:02d}", source="历史缓存") for index in range(20)],
                "历史缓存",
            )
            runtime = ProviderRuntime(cache, settings)
            coordinator = KlineCoordinator(
                settings=settings,
                cache=cache,
                providers={"primary": FailingKlineProvider()},
                runtime=runtime,
                priority=lambda kind: [(1, "primary")],
            )
            rows = await coordinator.kline("600519.SH", limit=20, use_cache=False)
            failure_count = next(item.failure_count for item in cache.provider_statuses() if item.name == "primary")
            return rows, runtime.is_cooling("primary", "kline"), failure_count

        with TemporaryDirectory() as tmpdir:
            rows, cooling, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(len(rows), 20)
        self.assertTrue(all(item.from_cache and item.fallback_used for item in rows))
        self.assertTrue(cooling)
        self.assertEqual(failure_count, 1)

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

    def test_metadata_coordinator_keeps_incomplete_stock_pool_miss_unknown(self) -> None:
        class TinyStockPoolProvider:
            source_name = "小样本股票池"

            async def stock_pool(self):
                return [_stock_info(code="600519", market="SH")]

        async def run_check(path: Path) -> tuple[str, int]:
            settings = Settings(stock_pool_authoritative_min_count=10)
            cache = SQLiteCache(path)
            runtime = ProviderRuntime(cache, settings)
            coordinator = MetadataCoordinator(
                settings=settings,
                cache=cache,
                providers={"tiny": TinyStockPoolProvider()},
                runtime=runtime,
                priority=lambda kind: [(1, "tiny")],
            )
            try:
                await coordinator.stock_pool(keyword="688001", limit=10, refresh=True)
            except Exception as exc:
                failure_count = next(item.failure_count for item in cache.provider_statuses() if item.name == "tiny")
                return exc.__class__.__name__, failure_count
            return "ok", 0

        with TemporaryDirectory() as tmpdir:
            error_name, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(error_name, "RuntimeError")
        self.assertEqual(failure_count, 0)

    def test_authoritative_fresh_stock_pool_cache_miss_skips_provider(self) -> None:
        class ExplodingStockPoolProvider:
            source_name = "不应调用股票池"

            def __init__(self) -> None:
                self.calls = 0

            async def stock_pool(self):
                self.calls += 1
                raise AssertionError("fresh authoritative cache should answer the miss")

        async def run_check(path: Path) -> tuple[list[str], int]:
            settings = Settings(stock_pool_cache_seconds=3600, stock_pool_authoritative_min_count=3)
            cache = SQLiteCache(path)
            provider = ExplodingStockPoolProvider()
            fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cache.save_stock_pool(
                [
                    _stock_info(code=f"60{index:04d}", market="SH").model_copy(update={"updated_at": fresh_time})
                    for index in range(3)
                ]
            )
            coordinator = MetadataCoordinator(
                settings=settings,
                cache=cache,
                providers={"exploding": provider},
                runtime=ProviderRuntime(cache, settings),
                priority=lambda kind: [(1, "exploding")],
            )
            rows = await coordinator.stock_pool(keyword="688001", limit=10, refresh=False)
            return [item.symbol for item in rows], provider.calls

        with TemporaryDirectory() as tmpdir:
            symbols, calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(symbols, [])
        self.assertEqual(calls, 0)

    def test_incomplete_stock_pool_keyword_miss_tries_next_provider(self) -> None:
        class TinyStockPoolProvider:
            source_name = "小样本股票池"

            def __init__(self) -> None:
                self.calls = 0

            async def stock_pool(self):
                self.calls += 1
                return [_stock_info(code="600519", market="SH")]

        class BackupStockPoolProvider:
            source_name = "备用股票池"

            def __init__(self) -> None:
                self.calls = 0

            async def stock_pool(self):
                self.calls += 1
                return [_stock_info(code="688001", market="SH"), _stock_info(code="000001", market="SZ")]

        async def run_check(path: Path) -> tuple[list[str], int, int]:
            settings = Settings(stock_pool_authoritative_min_count=10)
            cache = SQLiteCache(path)
            tiny = TinyStockPoolProvider()
            backup = BackupStockPoolProvider()
            coordinator = MetadataCoordinator(
                settings=settings,
                cache=cache,
                providers={"tiny": tiny, "backup": backup},
                runtime=ProviderRuntime(cache, settings),
                priority=lambda kind: [(1, "tiny"), (2, "backup")],
            )
            rows = await coordinator.stock_pool(keyword="688001", limit=10, refresh=True)
            return [item.symbol for item in rows], tiny.calls, backup.calls

        with TemporaryDirectory() as tmpdir:
            symbols, tiny_calls, backup_calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(symbols, ["688001.SH"])
        self.assertEqual(tiny_calls, 1)
        self.assertEqual(backup_calls, 1)

    def test_authoritative_stock_pool_miss_without_quote_confirmation_is_not_found(self) -> None:
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
                with patch.object(hub, "quote", side_effect=RuntimeError("quote unavailable")):
                    await individual._confirmed_stock_profile(hub, "688001.SH")
            except Exception as exc:
                return exc.__class__.__name__
            return "ok"

        with TemporaryDirectory() as tmpdir:
            error_name = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(error_name, NotFoundError.__name__)

    def test_authoritative_stock_pool_miss_can_be_confirmed_by_matching_quote(self) -> None:
        async def run_check(path: Path) -> tuple[str | None, list[str]]:
            hub = DataHub(cache=SQLiteCache(path))
            fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            hub.cache.save_stock_pool(
                [
                    _stock_info(code=f"60{index:04d}", market="SH").model_copy(update={"updated_at": fresh_time})
                    for index in range(1000)
                ]
            )
            quote = _quote().model_copy(update={"code": "688001", "market": "SH", "name": "测试新股"})
            with patch.object(hub, "quote", return_value=quote):
                profile = await individual._confirmed_stock_profile(hub, "688001.SH")
            with sqlite3.connect(path) as conn:
                events = [row[0] for row in conn.execute("SELECT message FROM cache_event ORDER BY id")]
            return profile.symbol if profile else None, events

        with TemporaryDirectory() as tmpdir:
            symbol, events = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(symbol, "688001.SH")
        self.assertTrue(any("股票池未命中，使用行情确认股票代码：688001.SH" in message for message in events))

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
