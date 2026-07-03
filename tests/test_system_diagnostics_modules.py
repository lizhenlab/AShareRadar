from __future__ import annotations

import unittest
import math
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.models.schemas import CacheStats, ProviderCapability, ProviderCapabilityStatus, ProviderStatus, SchedulerStatus
from app.services.system_diagnostics import (
    _provider_diagnostic_decision,
    age_seconds,
    build_system_diagnostics,
    capability_label,
    storage_diagnostics,
)


class SystemDiagnosticsModuleTests(unittest.TestCase):
    def test_diagnostics_reports_stale_cache_failed_capability_and_stopped_scheduler(self) -> None:
        checked_base = datetime.now()
        cache_stats = CacheStats(
            path="/tmp/ashare-radar-test.sqlite3",
            quote_count=1,
            quote_history_count=10,
            kline_count=20,
            stock_count=100,
            plate_count=5,
            provider_count=2,
            latest_quote_at=(checked_base - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S"),
            latest_kline_at=(checked_base - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
            latest_stock_at=checked_base.strftime("%Y-%m-%d %H:%M:%S"),
            latest_plate_at=checked_base.strftime("%Y-%m-%d %H:%M:%S"),
        )
        cache = _Cache(
            cache_stats,
            providers=[_provider_status("akshare", healthy=False)],
            capability_statuses=[_capability_status("akshare", "quote", healthy=False)],
            table_counts={"alert_rule": 1, "quote_history": 4, "stock_note": 2},
        )
        datahub = _DataHub(cache, capabilities=[_capability("tencent", realtime_quote=True)])
        scheduler = _Scheduler(running=False)

        with patch("app.services.system_diagnostics.calendar_source", return_value="交易日历缓存"):
            diagnostics = build_system_diagnostics(datahub, scheduler)

        self.assertTrue(any("最新报价缓存已超过" in item for item in diagnostics.warnings))
        self.assertIn("存在数据能力最近失败：akshare 报价", diagnostics.warnings)
        self.assertIn("可用实时报价源少于2个，多源一致性校验能力不足。", diagnostics.warnings)
        self.assertIn("日K线缓存超过1天未刷新，建议手动执行关键个股K线刷新。", diagnostics.suggestions)
        self.assertIn("存在本地预警但调度器未运行，建议启动调度器或手动评估。", diagnostics.suggestions)
        self.assertEqual(diagnostics.storage.runtime_rows, 4)
        self.assertEqual(diagnostics.storage.user_rows, 3)

    def test_diagnostics_reports_missing_quote_demo_source_and_calendar_fallback(self) -> None:
        cache = _Cache(
            CacheStats(
                path="/tmp/ashare-radar-test.sqlite3",
                quote_count=0,
                quote_history_count=0,
                kline_count=0,
                stock_count=0,
                plate_count=0,
                provider_count=1,
                latest_quote_at=None,
            ),
            providers=[],
            capability_statuses=[],
            table_counts={},
        )
        datahub = _DataHub(cache, capabilities=[_capability("demo", realtime_quote=True, reliability_level="演示")])

        with patch("app.services.system_diagnostics.calendar_source", return_value="工作日兜底"):
            diagnostics = build_system_diagnostics(datahub, _Scheduler(running=True))

        self.assertIn("尚未形成报价缓存。", diagnostics.warnings)
        self.assertIn("演示行情源已启用，当前环境不适合输出真实个股建议。", diagnostics.warnings)
        self.assertIn("交易日历未缓存，当前按普通工作日判断行情新鲜度。", diagnostics.warnings)
        self.assertIn("打开任意个股或手动执行刷新报价。", diagnostics.suggestions)

    def test_diagnostics_reports_future_cache_timestamps_as_invalid(self) -> None:
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        cache = _Cache(
            CacheStats(
                path="/tmp/ashare-radar-test.sqlite3",
                quote_count=1,
                quote_history_count=0,
                kline_count=1,
                stock_count=0,
                plate_count=0,
                provider_count=0,
                latest_quote_at=future,
                latest_kline_at=future,
            ),
            providers=[],
            capability_statuses=[],
            table_counts={},
        )
        datahub = _DataHub(cache, capabilities=[_capability("tencent", realtime_quote=True)])

        with patch("app.services.system_diagnostics.calendar_source", return_value="交易日历缓存"):
            diagnostics = build_system_diagnostics(datahub, _Scheduler(running=True))

        self.assertIsNone(diagnostics.freshness.latest_quote_age_seconds)
        self.assertIsNone(diagnostics.freshness.latest_kline_age_seconds)
        self.assertIn("最新报价缓存时间异常。", diagnostics.warnings)
        self.assertIn("最新K线缓存时间异常。", diagnostics.warnings)
        self.assertIn("检查系统时间、数据源时间字段或清理异常缓存。", diagnostics.suggestions)

    def test_diagnostics_deduplicates_messages_and_sanitizes_dirty_table_counts(self) -> None:
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        cache = _Cache(
            CacheStats(
                path="/tmp/ashare-radar-test.sqlite3",
                quote_count=1,
                quote_history_count=0,
                kline_count=1,
                stock_count=0,
                plate_count=0,
                provider_count=0,
                latest_quote_at=future,
                latest_kline_at=future,
            ),
            providers=[],
            capability_statuses=[],
            table_counts={
                "quote_history": "2.9",
                " quote_history ": 5,
                "watchlist": "bad",
                "alert_rule": -1,
                "stock_note": math.inf,
                "nan": 7,
            },
        )
        datahub = _DataHub(
            cache,
            capabilities=[_capability("tencent", realtime_quote=True), _capability(" tencent ", realtime_quote=True)],
        )

        with patch("app.services.system_diagnostics.calendar_source", return_value="交易日历缓存"):
            diagnostics = build_system_diagnostics(datahub, _Scheduler(running=True))

        self.assertEqual(diagnostics.suggestions.count("检查系统时间、数据源时间字段或清理异常缓存。"), 1)
        self.assertIn("可用实时报价源少于2个，多源一致性校验能力不足。", diagnostics.warnings)
        self.assertEqual(diagnostics.table_counts["quote_history"], 5)
        self.assertNotIn("nan", diagnostics.table_counts)
        self.assertEqual(diagnostics.storage.runtime_rows, 5)
        self.assertEqual(diagnostics.storage.user_rows, 0)

    def test_age_seconds_rejects_future_timestamps(self) -> None:
        self.assertEqual(age_seconds("2026-05-13 09:59:00", "2026-05-13 10:00:00"), 60)
        self.assertIsNone(age_seconds("2026-05-13 10:01:00", "2026-05-13 10:00:00"))

    def test_age_seconds_rejects_dirty_timestamp_values(self) -> None:
        self.assertIsNone(age_seconds(float("nan"), "2026-05-13 10:00:00"))  # type: ignore[arg-type]
        self.assertIsNone(age_seconds("2026-05-13 10:00:00", None))  # type: ignore[arg-type]

    def test_storage_diagnostics_counts_runtime_and_user_rows(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            path.write_bytes(b"x" * 1024)

            diagnostics = storage_diagnostics(
                path,
                {
                    "quote_history": 3,
                    "cache_event": 4,
                    "task_run": 2,
                    "monitor_event": 1,
                    "alert_event": 6,
                    "watchlist": 4,
                    "alert_rule": 5,
                    "stock_master": 9,
                },
            )

        self.assertEqual(diagnostics.db_size_bytes, 1024)
        self.assertEqual(diagnostics.runtime_rows, 16)
        self.assertEqual(diagnostics.user_rows, 9)

    def test_storage_diagnostics_sanitizes_malformed_table_counts(self) -> None:
        diagnostics = storage_diagnostics(
            Path("/tmp/not-created-ashare-radar.sqlite3"),
            {
                "quote_history": -3,
                "cache_event": "bad",
                "task_run": math.inf,
                "monitor_event": 2.9,
                "watchlist": 4.8,
                "alert_rule": math.nan,
                "stock_note": -1,
            },
        )

        self.assertEqual(diagnostics.runtime_rows, 2)
        self.assertEqual(diagnostics.user_rows, 4)

    def test_capability_label_maps_known_kinds_and_keeps_unknown(self) -> None:
        self.assertEqual(capability_label("minute"), "分钟线")
        self.assertEqual(capability_label("order_book"), "盘口")
        self.assertEqual(capability_label(" quote "), "报价")
        self.assertEqual(capability_label("custom"), "custom")
        self.assertEqual(capability_label(math.nan), "未知能力")  # type: ignore[arg-type]

    def test_provider_diagnostic_decision_prefers_and_caps_capability_failures(self) -> None:
        capabilities = [_capability_status(f"source{index}", "quote", healthy=False) for index in range(8)]

        decision = _provider_diagnostic_decision([_provider_status("aggregate", healthy=False)], capabilities)

        self.assertEqual(
            decision.warning,
            "存在数据能力最近失败：source0 报价、source1 报价、source2 报价、source3 报价、source4 报价、source5 报价",
        )
        self.assertEqual(decision.suggestion, "按失败能力检查网络、Token、本地客户端或源站连通性。")

    def test_provider_diagnostic_decision_ignores_disabled_capability_failures(self) -> None:
        decision = _provider_diagnostic_decision(
            [_provider_status("akshare", healthy=False)],
            [_capability_status("akshare", "quote", healthy=False, enabled=False)],
        )

        self.assertEqual(decision.warning, "存在数据源最近失败：akshare")
        self.assertEqual(decision.suggestion, "检查网络、Token 或数据源依赖安装状态。")

    def test_provider_diagnostic_decision_deduplicates_dirty_capability_failures(self) -> None:
        decision = _provider_diagnostic_decision(
            [],
            [
                SimpleNamespace(name=" akshare ", kind=" quote ", enabled=True, healthy=False, last_error="network down", failure_count=1),
                SimpleNamespace(name="akshare", kind="quote", enabled=True, healthy=False, last_error="network down", failure_count=2),
                SimpleNamespace(name="baostock", kind="kline", enabled=True, healthy=False, last_error=" ", failure_count=math.nan),
            ],
        )

        self.assertEqual(decision.warning, "存在数据能力最近失败：akshare 报价")
        self.assertEqual(decision.suggestion, "按失败能力检查网络、Token、本地客户端或源站连通性。")


class _Cache:
    def __init__(
        self,
        stats: CacheStats,
        *,
        providers: list[ProviderStatus],
        capability_statuses: list[ProviderCapabilityStatus],
        table_counts: dict[str, int],
    ) -> None:
        self._stats = stats
        self._providers = providers
        self._capability_statuses = capability_statuses
        self._table_counts = table_counts

    def stats(self) -> CacheStats:
        return self._stats

    def provider_statuses(self) -> list[ProviderStatus]:
        return self._providers

    def provider_capability_statuses(self) -> list[ProviderCapabilityStatus]:
        return self._capability_statuses

    def table_counts(self) -> dict[str, int]:
        return self._table_counts


class _DataHub:
    def __init__(self, cache: _Cache, *, capabilities: list[ProviderCapability]) -> None:
        self.cache = cache
        self._capabilities = capabilities

    def capabilities(self) -> list[ProviderCapability]:
        return self._capabilities


class _Scheduler:
    def __init__(self, *, running: bool) -> None:
        self._running = running

    def status(self) -> SchedulerStatus:
        return SchedulerStatus(enabled=True, running=self._running, started_at=None, task_count=0, tasks=[])


def _provider_status(name: str, *, healthy: bool) -> ProviderStatus:
    return ProviderStatus(
        name=name,
        enabled=True,
        priority=1,
        healthy=healthy,
        last_success=None,
        last_error=None if healthy else "network down",
        failure_count=0 if healthy else 1,
    )


def _capability_status(name: str, kind: str, *, healthy: bool, enabled: bool = True) -> ProviderCapabilityStatus:
    return ProviderCapabilityStatus(
        name=name,
        kind=kind,
        enabled=enabled,
        priority=1,
        healthy=healthy,
        last_error=None if healthy else "network down",
        failure_count=0 if healthy else 1,
    )


def _capability(name: str, *, realtime_quote: bool, reliability_level: str = "公开源") -> ProviderCapability:
    return ProviderCapability(
        name=name,
        installed=True,
        enabled=True,
        reliability_level=reliability_level,
        realtime_quote=realtime_quote,
        note="测试能力",
    )


if __name__ == "__main__":
    unittest.main()
