from __future__ import annotations

import unittest
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.models.schemas import CacheStats, ProviderCapability, ProviderCapabilityStatus, ProviderStatus, SchedulerStatus
from app.services.cache import SQLiteCache
from app.services.runtime_backup import create_runtime_backup, runtime_backup_storage
from app.services.trading_calendar import TradeCalendarSource, TradeCalendarStatus
from app.services.system_diagnostics import (
    _provider_diagnostic_decision,
    age_seconds,
    build_system_diagnostics,
    capability_label,
    storage_diagnostics,
)


class SystemDiagnosticsModuleTests(unittest.TestCase):
    def test_diagnostics_reports_stale_cache_failed_capability_and_stopped_scheduler(self) -> None:
        checked_base = datetime(2026, 5, 13, 10, 30, 0)
        fetched_at = "2026-05-13 10:29:30"
        cache_stats = CacheStats(
            path="/tmp/ashare-radar-test.sqlite3",
            quote_count=1,
            quote_history_count=10,
            kline_count=20,
            stock_count=100,
            plate_count=5,
            provider_count=2,
            latest_quote_at=fetched_at,
            latest_kline_at=fetched_at,
            latest_quote_fetched_at=fetched_at,
            latest_daily_kline_fetched_at=fetched_at,
            latest_quote_timestamp="2026-05-12 15:00:00",
            latest_daily_kline_date="2026-05-11",
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

        with patch("app.services.system_diagnostics.calendar_status", return_value=_calendar_status()):
            diagnostics = build_system_diagnostics(datahub, scheduler, now=checked_base)

        self.assertTrue(any("报价市场数据过期" in item for item in diagnostics.warnings))
        self.assertIn("存在数据能力最近失败：akshare 报价", diagnostics.warnings)
        self.assertIn("可用实时报价源少于2个，多源一致性校验能力不足。", diagnostics.warnings)
        self.assertIn("执行关键个股日K刷新，并检查数据源交易日期。", diagnostics.suggestions)
        self.assertIn("存在本地预警但调度器未运行，建议启动调度器或手动评估。", diagnostics.suggestions)
        self.assertEqual(diagnostics.freshness.fetch_activity["quote"].status, "recent")
        self.assertEqual(diagnostics.freshness.market_freshness["quote"].status, "stale")
        self.assertEqual(diagnostics.storage.cache_rows, 4)
        self.assertEqual(diagnostics.storage.runtime_rows, 0)
        self.assertEqual(diagnostics.storage.user_rows, 3)

    def test_diagnostics_reports_stale_daily_kline_even_when_minute_kline_is_fresh(self) -> None:
        checked_base = datetime(2026, 5, 13, 10, 30, 0)
        fetched_at = "2026-05-13 10:29:30"
        cache = _Cache(
            CacheStats(
                path="/tmp/ashare-radar-test.sqlite3",
                quote_count=1,
                quote_history_count=0,
                kline_count=2,
                daily_kline_count=1,
                minute_kline_count=1,
                stock_count=0,
                plate_count=1,
                provider_count=0,
                latest_quote_at=fetched_at,
                latest_kline_at=fetched_at,
                latest_daily_kline_at=fetched_at,
                latest_minute_kline_at=fetched_at,
                latest_quote_fetched_at=fetched_at,
                latest_daily_kline_fetched_at=fetched_at,
                latest_minute_kline_fetched_at=fetched_at,
                latest_quote_timestamp="2026-05-13 10:29:00",
                latest_daily_kline_date="2026-05-11",
                latest_minute_kline_timestamp="2026-05-13 10:29:00",
            ),
            providers=[],
            capability_statuses=[],
            table_counts={},
        )

        with patch("app.services.system_diagnostics.calendar_status", return_value=_calendar_status()):
            diagnostics = build_system_diagnostics(
                _DataHub(cache, capabilities=[]),
                _Scheduler(running=True),
                now=checked_base,
            )

        self.assertIn("执行关键个股日K刷新，并检查数据源交易日期。", diagnostics.suggestions)
        self.assertEqual(diagnostics.freshness.latest_minute_kline_age_seconds, 30)
        self.assertEqual(diagnostics.freshness.market_freshness["daily_kline"].status, "stale")
        self.assertEqual(diagnostics.freshness.market_freshness["minute_kline"].status, "fresh")

    def test_diagnostics_does_not_report_standby_scheduler_as_stopped(self) -> None:
        cache = _Cache(
            CacheStats(
                path=":memory:",
                quote_count=0,
                quote_history_count=0,
                kline_count=0,
                stock_count=0,
                plate_count=0,
                provider_count=0,
            ),
            providers=[],
            capability_statuses=[],
            table_counts={"alert_rule": 1},
        )
        scheduler = SimpleNamespace(
            status=lambda: SchedulerStatus(
                enabled=True,
                running=False,
                standby=True,
                task_count=0,
                tasks=[],
            )
        )

        with patch("app.services.system_diagnostics.calendar_status", return_value=_calendar_status()):
            diagnostics = build_system_diagnostics(_DataHub(cache, capabilities=[]), scheduler)

        self.assertNotIn("存在本地预警但调度器未运行，建议启动调度器或手动评估。", diagnostics.suggestions)

    def test_diagnostics_reports_demo_source_and_unavailable_calendar(self) -> None:
        now = datetime(2026, 5, 13, 10, 30, 0)
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

        with patch(
            "app.services.system_diagnostics.calendar_status",
            return_value=_calendar_status(TradeCalendarSource.UNAVAILABLE, covered=False),
        ):
            diagnostics = build_system_diagnostics(datahub, _Scheduler(running=True), now=now)

        self.assertIn("演示行情源已启用，当前环境不适合输出真实个股建议。", diagnostics.warnings)
        self.assertIn("运行时交易日历与内置基线均不可用，已跳过行情交易日期判断并保守关闭交易任务。", diagnostics.warnings)
        self.assertIn("检查 app/resources/trading_calendar.json 完整性，并调用交易日历刷新 API 重建 data/ 运行时缓存。", diagnostics.suggestions)
        self.assertEqual(diagnostics.freshness.checked_domains, [])

    def test_diagnostics_reports_out_of_coverage_calendar_with_annual_maintenance_action(self) -> None:
        now = datetime(2027, 1, 4, 10, 30, 0)
        cache = _Cache(
            CacheStats(
                path=":memory:",
                quote_count=1,
                quote_history_count=0,
                kline_count=1,
                stock_count=0,
                plate_count=0,
                provider_count=0,
                latest_quote_timestamp="2026-12-31 15:00:00",
                latest_daily_kline_date="2026-12-31",
            ),
            providers=[],
            capability_statuses=[],
            table_counts={},
        )

        with patch(
            "app.services.system_diagnostics.calendar_status",
            return_value=_calendar_status(TradeCalendarSource.OUT_OF_COVERAGE, covered=False),
        ):
            diagnostics = build_system_diagnostics(
                _DataHub(cache, capabilities=[]),
                _Scheduler(running=True),
                now=now,
            )

        self.assertIn("交易日历未覆盖当前日期，已跳过依赖交易日期的行情新鲜度判断并保守关闭交易任务。", diagnostics.warnings)
        self.assertIn("调用 POST /api/data/trading-calendar/refresh 刷新运行时日历；进入新年度前同时更新 bundled baseline。", diagnostics.suggestions)
        self.assertEqual(diagnostics.freshness.market_freshness, {})

    def test_diagnostics_reports_ignored_runtime_while_bundle_remains_usable(self) -> None:
        cache = _Cache(
            CacheStats(
                path=":memory:",
                quote_count=0,
                quote_history_count=0,
                kline_count=0,
                stock_count=0,
                plate_count=0,
                provider_count=0,
            ),
            providers=[],
            capability_statuses=[],
            table_counts={},
        )
        calendar_warning = "运行时交易日历 JSON 损坏，已忽略该快照。"

        with patch(
            "app.services.system_diagnostics.calendar_status",
            return_value=_calendar_status(TradeCalendarSource.BUNDLED_BASELINE, warning=calendar_warning),
        ):
            diagnostics = build_system_diagnostics(
                _DataHub(cache, capabilities=[]),
                _Scheduler(running=True),
                now=datetime(2026, 5, 13, 10, 30, 0),
            )

        self.assertIn(calendar_warning, diagnostics.warnings)
        self.assertIn("调用 POST /api/data/trading-calendar/refresh 重建运行时交易日历缓存。", diagnostics.suggestions)

    def test_diagnostics_reports_future_market_timestamps_separately_from_fetch_activity(self) -> None:
        now = datetime(2026, 5, 13, 10, 30, 0)
        fetched_at = "2026-05-13 10:29:30"
        cache = _Cache(
            CacheStats(
                path="/tmp/ashare-radar-test.sqlite3",
                quote_count=1,
                quote_history_count=0,
                kline_count=1,
                stock_count=0,
                plate_count=1,
                provider_count=0,
                latest_quote_at=fetched_at,
                latest_kline_at=fetched_at,
                latest_quote_fetched_at=fetched_at,
                latest_daily_kline_fetched_at=fetched_at,
                latest_quote_timestamp="2026-05-14 10:00:00",
                latest_daily_kline_date="2026-05-14",
            ),
            providers=[],
            capability_statuses=[],
            table_counts={},
        )
        datahub = _DataHub(cache, capabilities=[_capability("tencent", realtime_quote=True)])

        with patch("app.services.system_diagnostics.calendar_status", return_value=_calendar_status()):
            diagnostics = build_system_diagnostics(datahub, _Scheduler(running=True), now=now)

        self.assertEqual(diagnostics.freshness.latest_quote_age_seconds, 30)
        self.assertEqual(diagnostics.freshness.latest_kline_age_seconds, 30)
        self.assertEqual(diagnostics.freshness.fetch_activity["quote"].status, "recent")
        self.assertEqual(diagnostics.freshness.market_freshness["quote"].status, "future")
        self.assertEqual(diagnostics.freshness.market_freshness["daily_kline"].status, "future")
        self.assertIn("报价市场事件时间 2026-05-14 10:00:00 晚于检查时间。", diagnostics.warnings)
        self.assertIn("日K市场日期 2026-05-14 晚于检查日期。", diagnostics.warnings)

    def test_diagnostics_reports_future_minute_kline_market_timestamp(self) -> None:
        now = datetime(2026, 5, 13, 10, 30, 0)
        fetched_at = "2026-05-13 10:29:30"
        cache = _Cache(
            CacheStats(
                path="/tmp/ashare-radar-test.sqlite3",
                quote_count=1,
                quote_history_count=0,
                kline_count=2,
                daily_kline_count=1,
                minute_kline_count=1,
                stock_count=0,
                plate_count=1,
                provider_count=0,
                latest_quote_at=fetched_at,
                latest_kline_at=fetched_at,
                latest_daily_kline_at=fetched_at,
                latest_minute_kline_at=fetched_at,
                latest_quote_fetched_at=fetched_at,
                latest_daily_kline_fetched_at=fetched_at,
                latest_minute_kline_fetched_at=fetched_at,
                latest_quote_timestamp="2026-05-13 10:29:00",
                latest_daily_kline_date="2026-05-12",
                latest_minute_kline_timestamp="2026-05-14 10:00:00",
            ),
            providers=[],
            capability_statuses=[],
            table_counts={},
        )
        datahub = _DataHub(
            cache,
            capabilities=[_capability("tencent", realtime_quote=True), _capability("futu", realtime_quote=True)],
        )

        with patch("app.services.system_diagnostics.calendar_status", return_value=_calendar_status()):
            diagnostics = build_system_diagnostics(datahub, _Scheduler(running=True), now=now)

        self.assertEqual(diagnostics.freshness.latest_minute_kline_age_seconds, 30)
        self.assertEqual(diagnostics.freshness.market_freshness["minute_kline"].status, "future")
        self.assertIn("分钟K市场事件时间 2026-05-14 10:00:00 晚于检查时间。", diagnostics.warnings)
        self.assertIn("刷新分钟K线，并检查数据源返回的市场事件时间。", diagnostics.suggestions)

    def test_diagnostics_deduplicates_messages_and_sanitizes_dirty_table_counts(self) -> None:
        now = datetime(2026, 5, 13, 10, 30, 0)
        future = "2026-05-14 10:00:00"
        cache = _Cache(
            CacheStats(
                path="/tmp/ashare-radar-test.sqlite3",
                quote_count=1,
                quote_history_count=0,
                kline_count=1,
                stock_count=0,
                plate_count=1,
                provider_count=0,
                latest_quote_at=future,
                latest_kline_at=future,
                latest_quote_fetched_at=future,
                latest_daily_kline_fetched_at=future,
                latest_quote_timestamp="2026-05-13 10:29:00",
                latest_daily_kline_date="2026-05-12",
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

        with patch("app.services.system_diagnostics.calendar_status", return_value=_calendar_status()):
            diagnostics = build_system_diagnostics(datahub, _Scheduler(running=True), now=now)

        self.assertEqual(diagnostics.suggestions.count("检查系统时间、抓取时间字段或清理异常缓存。"), 1)
        self.assertIn("可用实时报价源少于2个，多源一致性校验能力不足。", diagnostics.warnings)
        self.assertEqual(diagnostics.table_counts["quote_history"], 5)
        self.assertNotIn("nan", diagnostics.table_counts)
        self.assertEqual(diagnostics.storage.cache_rows, 5)
        self.assertEqual(diagnostics.storage.runtime_rows, 0)
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
                    "kline_daily": 5,
                    "cache_event": 4,
                    "task_run": 2,
                    "monitor_event": 1,
                    "market_scan_run": 7,
                    "market_scan_result": 11,
                    "alert_event": 6,
                    "watchlist": 4,
                    "alert_rule": 5,
                    "stock_master": 9,
                },
            )

        self.assertEqual(diagnostics.db_size_bytes, 1024)
        self.assertEqual(diagnostics.sqlite_size_bytes, 1024)
        self.assertEqual(diagnostics.backup_size_bytes, 0)
        self.assertEqual(diagnostics.managed_backup_count, 0)
        self.assertEqual(diagnostics.cache_rows, 17)
        self.assertEqual(diagnostics.runtime_rows, 25)
        self.assertEqual(diagnostics.user_rows, 15)
        self.assertEqual(diagnostics.quote_rows, 3)
        self.assertEqual(diagnostics.kline_rows, 5)
        self.assertEqual(diagnostics.market_scan_rows, 18)
        self.assertEqual(diagnostics.other_cache_rows, 9)
        self.assertEqual(diagnostics.other_runtime_rows, 7)
        self.assertEqual(diagnostics.budget_bytes, 512 * 1024 * 1024)
        self.assertFalse(diagnostics.over_budget)

    def test_storage_diagnostics_includes_active_wal_and_shm_files(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            path.write_bytes(b"x" * 1024)
            Path(f"{path}-wal").write_bytes(b"w" * 2048)
            Path(f"{path}-shm").write_bytes(b"s" * 4096)

            diagnostics = storage_diagnostics(path, {})

        self.assertEqual(diagnostics.db_size_bytes, 1024 + 2048 + 4096)
        self.assertEqual(
            diagnostics.usage_pct,
            round(diagnostics.db_size_bytes / diagnostics.budget_bytes * 100, 2),
        )

    def test_storage_diagnostics_reports_managed_backup_bytes_and_count(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runtime.sqlite3"
            SQLiteCache(path)
            create_runtime_backup(path, max_backups=2)
            backup_storage = runtime_backup_storage(path)
            sqlite_bytes = sum(
                candidate.stat().st_size
                for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
                if candidate.exists()
            )

            diagnostics = storage_diagnostics(path, {})

        self.assertEqual(backup_storage.managed_bundle_count, 1)
        self.assertGreater(backup_storage.size_bytes, 0)
        self.assertEqual(diagnostics.db_size_bytes, sqlite_bytes + backup_storage.size_bytes)
        self.assertEqual(diagnostics.sqlite_size_bytes, sqlite_bytes)
        self.assertEqual(diagnostics.backup_size_bytes, backup_storage.size_bytes)
        self.assertEqual(diagnostics.managed_backup_count, 1)

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

    def test_storage_diagnostics_reports_configured_budget_pressure(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            path.write_bytes(b"x" * 1024)

            diagnostics = storage_diagnostics(path, {}, budget_mb=0.0005)

        self.assertEqual(diagnostics.budget_bytes, 512 * 1024 * 1024)
        self.assertFalse(diagnostics.over_budget)

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            path.write_bytes(b"x" * 17 * 1024 * 1024)
            diagnostics = storage_diagnostics(path, {}, budget_mb=16)

        self.assertTrue(diagnostics.over_budget)
        self.assertGreater(diagnostics.usage_pct, 100)

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

    def test_provider_diagnostic_decision_ignores_stale_failures(self) -> None:
        stale_at = (datetime.now() - timedelta(minutes=31)).strftime("%Y-%m-%d %H:%M:%S")

        decision = _provider_diagnostic_decision(
            [_provider_status("akshare", healthy=False, updated_at=stale_at)],
            [_capability_status("baostock", "kline", healthy=False, updated_at=stale_at)],
        )

        self.assertIsNone(decision.warning)
        self.assertIsNone(decision.suggestion)

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


def _calendar_status(
    source: TradeCalendarSource = TradeCalendarSource.RUNTIME_CACHE,
    *,
    covered: bool = True,
    warning: str | None = None,
) -> TradeCalendarStatus:
    return TradeCalendarStatus(
        target_date=date(2026, 5, 13),
        source=source,
        covered=covered,
        min_date=date(1990, 12, 19) if covered else None,
        max_date=date(2026, 12, 31) if covered else None,
        warning=warning,
    )


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


def _provider_status(name: str, *, healthy: bool, updated_at: str | None = None) -> ProviderStatus:
    return ProviderStatus(
        name=name,
        enabled=True,
        priority=1,
        healthy=healthy,
        last_success=None,
        last_error=None if healthy else "network down",
        failure_count=0 if healthy else 1,
        updated_at=updated_at,
    )


def _capability_status(
    name: str,
    kind: str,
    *,
    healthy: bool,
    enabled: bool = True,
    updated_at: str | None = None,
) -> ProviderCapabilityStatus:
    return ProviderCapabilityStatus(
        name=name,
        kind=kind,
        enabled=enabled,
        priority=1,
        healthy=healthy,
        last_error=None if healthy else "network down",
        failure_count=0 if healthy else 1,
        updated_at=updated_at,
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
