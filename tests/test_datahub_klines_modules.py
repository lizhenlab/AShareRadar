from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import math
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time

import pytest

from app.config import Settings
from app.models.schemas import Kline, MinuteKline
from app.services.cache import SQLiteCache
from app.services.datahub_klines import (
    DEFAULT_MAX_MINUTE_KLINE_LIMIT,
    MAX_DAILY_KLINE_LIMIT,
    KlineCoordinator,
    _bounded_limit,
    _kline_now as production_kline_now,
    _latest_minute_klines,
    _prepare_daily_klines,
)
from app.services.datahub_runtime import ProviderRuntime
from app.services.provider_errors import (
    ProviderChainUnavailable,
    ProviderInstrumentDataError,
    ProviderProtocolError,
    ProviderTransportError,
)
from app.utils.market_time import market_now_naive
from app.utils.time import now_text
from tests.factories import make_kline


KLINE_TEST_NOW = datetime(2026, 5, 13, 10, 20, 0)


@pytest.fixture(autouse=True)
def _fixed_kline_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.datahub_klines._kline_now", lambda: KLINE_TEST_NOW)


def test_latest_minute_rows_compare_aware_and_naive_times_in_shanghai_timezone() -> None:
    original_timezone = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "UTC"
        time.tzset()
        rows = [
            _minute_row(timestamp="2026-05-13 09:31:00", interval="1m"),
            _minute_row(timestamp="2026-05-13T01:32:00+00:00", interval="1m"),
        ]

        latest = _latest_minute_klines(rows, 1)
    finally:
        if original_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()

    assert [row.timestamp for row in latest] == ["2026-05-13T01:32:00+00:00"]


def test_kline_production_clock_is_independent_of_host_timezone() -> None:
    original_timezone = os.environ.get("TZ")
    snapshots: list[datetime] = []
    try:
        for timezone_name in ("UTC", "Asia/Shanghai"):
            os.environ["TZ"] = timezone_name
            time.tzset()
            snapshots.append(production_kline_now())
    finally:
        if original_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()

    assert all(item.tzinfo is None for item in snapshots)
    assert abs((snapshots[1] - snapshots[0]).total_seconds()) < 1


def test_daily_kline_coverage_miss_uses_fallback_without_global_failure() -> None:
    class EmptyKlineProvider:
        source_name = "空K线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return []

    async def run_check(path: Path) -> tuple[list[Kline], bool, bool, int, int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        cache.save_klines(
            "600519.SH",
            [make_kline(date=f"2026-05-{index + 1:02d}", source="历史缓存") for index in range(20)],
            "历史缓存",
        )
        runtime = ProviderRuntime(cache, settings)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"empty": EmptyKlineProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "empty")],
        )

        rows = await coordinator.kline("600519.SH", limit=20, use_cache=False)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "empty" and item.kind == "kline")
        return rows, runtime.is_cooling("empty", "kline"), status.healthy, status.success_count, status.failure_count

    with TemporaryDirectory() as tmpdir:
        rows, cooling, healthy, success_count, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert len(rows) == 20
    assert all(item.from_cache and item.fallback_used for item in rows)
    assert cooling is False
    assert healthy is True
    assert success_count == 1
    assert failure_count == 0


def test_daily_kline_reports_temporarily_unavailable_chain_without_cache() -> None:
    class CoolingProvider:
        source_name = "冷却日线源"

        def __init__(self) -> None:
            self.calls = 0

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls += 1
            return [make_kline(date="2026-05-13")]

    async def run_check(path: Path) -> int:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        provider = CoolingProvider()
        runtime = ProviderRuntime(cache, settings)
        runtime.record_failure("cooling", 1, RuntimeError("源站限流"), "kline")
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"cooling": provider},
            runtime=runtime,
            priority=lambda kind: [(1, "cooling")],
        )

        with pytest.raises(ProviderChainUnavailable, match="当前暂不可用"):
            await coordinator.kline("600519.SH", limit=20, use_cache=False)
        return provider.calls

    with TemporaryDirectory() as tmpdir:
        calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == 0


def test_required_daily_kline_reports_permanently_unavailable_chain_before_cache_fallback() -> None:
    async def run_check(path: Path) -> float | None:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        cache.save_klines(
            "600519.SH",
            [make_kline(date="2026-05-12", source="旧缓存")],
            "旧缓存",
        )
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "missing")],
            now=lambda: datetime(2026, 5, 13, 16, 0, 0),
        )

        with pytest.raises(ProviderChainUnavailable, match="数据源未注册") as exc_info:
            await coordinator.kline(
                "600519.SH",
                limit=20,
                use_cache=False,
                allow_stale=True,
                require_provider_response=True,
            )
        return exc_info.value.retry_after_seconds

    with TemporaryDirectory() as tmpdir:
        retry_after_seconds = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert retry_after_seconds is None


def test_required_daily_kline_treats_full_provider_capacity_as_retryable() -> None:
    class BusyProvider:
        source_name = "繁忙日线源"

        def __init__(self) -> None:
            self.calls = 0

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            del symbol, limit
            self.calls += 1
            return [make_kline(date="2026-05-13")]

    async def run_check(path: Path) -> tuple[float | None, int, bool]:
        settings = Settings(
            provider_call_timeout_seconds=0.02,
            provider_failure_cooldown_seconds=60,
        )
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        provider = BusyProvider()
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"busy": provider},
            runtime=runtime,
            priority=lambda kind: [(1, "busy")],
        )
        release = asyncio.Event()
        capacity_reached = asyncio.Event()
        started = 0

        async def blocking_call() -> None:
            nonlocal started
            started += 1
            if started == 2:
                capacity_reached.set()
            await release.wait()

        blockers = [
            asyncio.create_task(
                runtime.call_provider(
                    "busy",
                    "kline",
                    blocking_call,
                    request_key=f"block-{index}",
                    timeout_seconds=1,
                )
            )
            for index in range(2)
        ]
        try:
            await asyncio.wait_for(capacity_reached.wait(), timeout=0.2)
            with pytest.raises(ProviderChainUnavailable) as exc_info:
                await coordinator.kline(
                    "600519.SH",
                    limit=20,
                    use_cache=False,
                    require_provider_response=True,
                )
            return (
                exc_info.value.retry_after_seconds,
                provider.calls,
                runtime.is_cooling("busy", "kline"),
            )
        finally:
            release.set()
            await asyncio.gather(*blockers, return_exceptions=True)
            await runtime.aclose()

    with TemporaryDirectory() as tmpdir:
        retry_after_seconds, provider_calls, cooling = asyncio.run(
            run_check(Path(tmpdir) / "cache.sqlite3")
        )

    assert retry_after_seconds is not None
    assert retry_after_seconds > 0
    assert provider_calls == 0
    assert cooling is False


def test_daily_kline_transport_failure_is_retryable_even_when_cooldown_is_disabled() -> None:
    class FailingProvider:
        source_name = "失败日线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            del symbol, limit
            raise ProviderTransportError("连接被重置")

    async def run_check(path: Path) -> None:
        settings = Settings(provider_failure_cooldown_seconds=0)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"failing": FailingProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "failing")],
        )

        with pytest.raises(ProviderChainUnavailable, match="所有日K数据源"):
            await coordinator.kline("600519.SH", limit=20, use_cache=False)

    with TemporaryDirectory() as tmpdir:
        asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))


def test_daily_kline_protocol_failure_is_retryable_even_when_cooldown_is_disabled() -> None:
    class MalformedProvider:
        source_name = "异常协议日线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            del symbol, limit
            raise ProviderProtocolError("响应顶层结构异常")

    async def run_check(path: Path) -> None:
        settings = Settings(provider_failure_cooldown_seconds=0)
        cache = SQLiteCache(path)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"malformed": MalformedProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "malformed")],
        )

        with pytest.raises(ProviderChainUnavailable, match="所有日K数据源"):
            await coordinator.kline(
                "600519.SH",
                limit=20,
                use_cache=False,
                require_provider_response=True,
            )

    with TemporaryDirectory() as tmpdir:
        asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))


def test_daily_kline_instrument_error_does_not_mark_the_chain_unavailable() -> None:
    class InvalidInstrumentProvider:
        source_name = "单标的异常源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            del symbol, limit
            raise ProviderInstrumentDataError("当前股票字段损坏")

    async def run_check(path: Path) -> tuple[bool, int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"invalid": InvalidInstrumentProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "invalid")],
        )

        with pytest.raises(RuntimeError, match="所有K线数据源均不可用"):
            await coordinator.kline("600519.SH", limit=20, use_cache=False)
        status = next(
            item
            for item in cache.provider_capability_statuses()
            if item.name == "invalid" and item.kind == "kline"
        )
        return runtime.is_cooling("invalid", "kline"), status.failure_count

    with TemporaryDirectory() as tmpdir:
        cooling, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert cooling is False
    assert failure_count == 1


def test_daily_kline_allows_stale_rows_only_for_explicit_validation_callers() -> None:
    class StaleProvider:
        def __init__(self, source_name: str, latest: str) -> None:
            self.source_name = source_name
            self.latest = latest

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            del symbol, limit
            return [make_kline(date=self.latest)]

    async def run_check(path: Path) -> list[Kline]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={
                "newer": StaleProvider("较新停牌日线", "2026-05-12"),
                "older": StaleProvider("较旧停牌日线", "2026-05-09"),
            },
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "newer"), (2, "older")],
            now=lambda: datetime(2026, 5, 13, 16, 0, 0),
        )

        return await coordinator.kline(
            "600519.SH",
            limit=20,
            use_cache=False,
            allow_stale=True,
            require_provider_response=True,
        )

    with TemporaryDirectory() as tmpdir:
        rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert rows[-1].date == "2026-05-12"
    assert rows[-1].source == "较新停牌日线"


def test_market_scan_policy_does_not_hide_provider_outage_behind_old_cache() -> None:
    class FailingProvider:
        source_name = "故障日线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            del symbol, limit
            raise ProviderTransportError("网络断开")

    async def run_check(path: Path) -> None:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        cache.save_klines(
            "600519.SH",
            [make_kline(date="2026-05-12", source="旧缓存")],
            "旧缓存",
        )
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"failing": FailingProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "failing")],
            now=lambda: datetime(2026, 5, 13, 16, 0, 0),
        )

        with pytest.raises(ProviderChainUnavailable, match="当前暂不可用"):
            await coordinator.kline(
                "600519.SH",
                limit=20,
                use_cache=False,
                allow_stale=True,
                require_provider_response=True,
            )

    with TemporaryDirectory() as tmpdir:
        asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))


def test_daily_kline_none_response_is_protocol_failure_not_coverage_miss() -> None:
    class MalformedKlineProvider:
        source_name = "坏结构K线源"

        async def kline(self, symbol: str, limit: int = 120):
            return None

    class BackupKlineProvider:
        source_name = "备用K线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date="2026-05-13")]

    async def run_check(path: Path) -> tuple[list[Kline], bool, int, str | None]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"malformed": MalformedKlineProvider(), "backup": BackupKlineProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "malformed"), (2, "backup")],
        )

        rows = await coordinator.kline("600519.SH", limit=20, use_cache=False)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "malformed" and item.kind == "kline")
        return rows, runtime.is_cooling("malformed", "kline"), status.failure_count, status.last_error

    with TemporaryDirectory() as tmpdir:
        rows, cooling, failure_count, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert rows[0].source == "备用K线源"
    assert rows[0].fallback_used is True
    assert cooling is True
    assert failure_count == 1
    assert last_error == "坏结构K线源 日K返回结构异常"


def test_backup_daily_klines_remain_fallback_when_second_request_uses_only_cache() -> None:
    end_date = KLINE_TEST_NOW.date()

    class FailingPrimaryProvider:
        source_name = "失败主日线源"

        def __init__(self) -> None:
            self.calls = 0

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls += 1
            raise RuntimeError("主源不可用")

    class BackupProvider:
        source_name = "备用日线源"

        def __init__(self) -> None:
            self.calls = 0

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls += 1
            return [
                make_kline(
                    date=(end_date - timedelta(days=19 - index)).isoformat(),
                    as_of=end_date.isoformat(),
                    data_version="backup-daily-version-1",
                )
                for index in range(20)
            ][-limit:]

    async def run_check(path: Path) -> tuple[list[Kline], list[Kline], int, int]:
        settings = Settings(kline_cache_seconds=3600)
        cache = SQLiteCache(path)
        primary = FailingPrimaryProvider()
        backup = BackupProvider()
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"primary": primary, "backup": backup},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "primary"), (2, "backup")],
            now=lambda: datetime(2026, 5, 13, 16, 0, 0),
        )

        first = await coordinator.kline("600519.SH", limit=20, use_cache=False)
        second = await coordinator.kline("600519.SH", limit=20, use_cache=True)
        return first, second, primary.calls, backup.calls

    with TemporaryDirectory() as tmpdir:
        first, second, primary_calls, backup_calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert primary_calls == backup_calls == 1
    assert all(item.fallback_used and not item.from_cache for item in first)
    assert all(item.fallback_used and item.from_cache for item in second)


def test_stale_primary_daily_kline_uses_fresh_backup_before_success_or_save() -> None:
    current = datetime(2026, 5, 13, 16, 0, 0)

    class StaleDailyProvider:
        source_name = "旧日线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date="2026-05-12")]

    class FreshDailyProvider:
        source_name = "新日线后备源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date="2026-05-13")]

    async def run_check(path: Path) -> tuple[list[Kline], tuple[int, int, str | None], tuple[int, int]]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"stale": StaleDailyProvider(), "backup": FreshDailyProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "stale"), (2, "backup")],
            now=lambda: current,
        )

        rows = await coordinator.kline("600519.SH", limit=20, use_cache=False)
        statuses = {item.name: item for item in cache.provider_capability_statuses() if item.kind == "kline"}
        stale = statuses["stale"]
        backup = statuses["backup"]
        return (
            rows,
            (stale.success_count, stale.failure_count, stale.last_error),
            (backup.success_count, backup.failure_count),
        )

    with TemporaryDirectory() as tmpdir:
        rows, stale_status, backup_status = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["新日线后备源"]
    assert all(not item.from_cache and item.fallback_used for item in rows)
    assert stale_status == (1, 0, None)
    assert backup_status == (1, 0)


def test_daily_kline_fallback_cache_ignores_log_event_failure() -> None:
    class LogFailingSQLiteCache(SQLiteCache):
        def log_event(self, category: str, message: str) -> None:
            raise sqlite3.DatabaseError("event log readonly")

    class FailingKlineProvider:
        source_name = "失败K线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            raise RuntimeError("kline down")

    async def run_check(path: Path) -> list[Kline]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = LogFailingSQLiteCache(path)
        cache.save_klines(
            "600519.SH",
            [make_kline(date=f"2026-05-{index + 1:02d}", source="历史缓存") for index in range(20)],
            "历史缓存",
        )
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"failing": FailingKlineProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "failing")],
        )

        return await coordinator.kline("600519.SH", limit=20, use_cache=False)

    with TemporaryDirectory() as tmpdir:
        rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert len(rows) == 20
    assert all(item.from_cache and item.fallback_used for item in rows)


def test_minute_kline_records_provider_without_minute_method_and_uses_backup() -> None:
    class QuoteOnlyProvider:
        source_name = "只有行情源"

    class BackupMinuteProvider:
        source_name = "备用分钟线源"

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            return [_minute_row(timestamp="2026-05-13 10:15:00", interval=interval)]

    async def run_check(path: Path) -> tuple[list[MinuteKline], bool, int, str | None]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"quote_only": QuoteOnlyProvider(), "backup": BackupMinuteProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "quote_only"), (2, "backup")],
        )

        rows = await coordinator.minute_kline("600519.SH", interval="5", limit=10, use_cache=False)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "quote_only" and item.kind == "minute")
        return rows, runtime.is_cooling("quote_only", "minute"), status.failure_count, status.last_error

    with TemporaryDirectory() as tmpdir:
        rows, cooling, failure_count, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["备用分钟线源"]
    assert [item.interval for item in rows] == ["5m"]
    assert cooling is True
    assert failure_count == 1
    assert last_error == "数据源不支持分钟K能力"


def test_minute_kline_coverage_miss_uses_fallback_without_global_failure() -> None:
    class EmptyMinuteProvider:
        source_name = "空分钟线源"

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            return []

    async def run_check(path: Path) -> tuple[list[MinuteKline], bool, bool, int, int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        cache.save_minute_klines(
            "600519.SH",
            "5m",
            [_minute_row(timestamp=f"2026-05-13 10:{index:02d}:00", interval="5m") for index in range(12)],
            "历史分钟缓存",
        )
        runtime = ProviderRuntime(cache, settings)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"empty": EmptyMinuteProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "empty")],
        )

        rows = await coordinator.minute_kline("600519.SH", interval="5m", limit=12, use_cache=False)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "empty" and item.kind == "minute")
        return rows, runtime.is_cooling("empty", "minute"), status.healthy, status.success_count, status.failure_count

    with TemporaryDirectory() as tmpdir:
        rows, cooling, healthy, success_count, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert len(rows) == 12
    assert all(item.from_cache and item.fallback_used for item in rows)
    assert cooling is False
    assert healthy is True
    assert success_count == 1
    assert failure_count == 0


def test_minute_kline_fallback_provenance_roundtrips_with_false_default() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        cache.save_minute_klines(
            "600519.SH",
            "5m",
            [
                _minute_row(timestamp="2026-05-13 10:10:00", interval="5m"),
                _minute_row(timestamp="2026-05-13 10:15:00", interval="5m").model_copy(
                    update={"fallback_used": True}
                ),
            ],
            "分钟线源",
        )
        loaded = cache.get_minute_klines(
            "600519.SH",
            "5m",
            limit=10,
            max_age_seconds=10**9,
        )
        with sqlite3.connect(path) as conn:
            persisted = [
                int(row[0])
                for row in conn.execute(
                    "SELECT fallback_used FROM kline_minute ORDER BY timestamp"
                )
            ]

    assert [item.fallback_used for item in loaded] == [False, True]
    assert all(item.from_cache for item in loaded)
    assert persisted == [0, 1]


def test_stale_primary_minute_kline_uses_fresh_backup_before_success_or_save() -> None:
    current = datetime(2026, 5, 13, 16, 0, 0)

    class StaleMinuteProvider:
        source_name = "旧分钟线源"

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            return [_minute_row(timestamp="2026-05-13 14:30:00", interval=interval)]

    class FreshMinuteProvider:
        source_name = "新分钟线后备源"

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            return [_minute_row(timestamp="2026-05-13 14:55:00", interval=interval)]

    async def run_check(
        path: Path,
    ) -> tuple[
        list[MinuteKline],
        list[MinuteKline],
        tuple[int, int, str | None],
        tuple[int, int],
    ]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"stale": StaleMinuteProvider(), "backup": FreshMinuteProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "stale"), (2, "backup")],
            now=lambda: current,
        )

        rows = await coordinator.minute_kline("600519.SH", interval="5m", limit=20, use_cache=False)
        cached = cache.get_minute_klines("600519.SH", "5m", limit=20, max_age_seconds=10**9)
        statuses = {item.name: item for item in cache.provider_capability_statuses() if item.kind == "minute"}
        stale = statuses["stale"]
        backup = statuses["backup"]
        return (
            rows,
            cached,
            (stale.success_count, stale.failure_count, stale.last_error),
            (backup.success_count, backup.failure_count),
        )

    with TemporaryDirectory() as tmpdir:
        rows, cached, stale_status, backup_status = asyncio.run(
            run_check(Path(tmpdir) / "cache.sqlite3")
        )

    assert [item.source for item in rows] == ["新分钟线后备源"]
    assert all(not item.from_cache and item.fallback_used for item in rows)
    assert all(item.from_cache and item.fallback_used for item in cached)
    assert stale_status == (0, 1, "旧分钟线源 分钟K线业务时间无效或已过期：2026-05-13 14:30:00")
    assert backup_status == (1, 0)


def test_all_stale_kline_sources_preserve_real_cache_fetched_at_and_mark_fallback() -> None:
    current = datetime(2026, 5, 13, 16, 0, 0)

    class StaleProvider:
        def __init__(self, source_name: str, daily_date: str, minute_timestamp: str) -> None:
            self.source_name = source_name
            self.daily_date = daily_date
            self.minute_timestamp = minute_timestamp

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date=self.daily_date, close=200.0)]

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            return [_minute_row(timestamp=self.minute_timestamp, interval=interval).model_copy(update={"close": 200.0, "high": 201.0})]

    async def run_check(path: Path):
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        cache.save_klines(
            "600519.SH",
            [make_kline(date="2026-05-12", source="真实旧缓存")],
            "真实旧缓存",
        )
        cache.save_minute_klines(
            "600519.SH",
            "5m",
            [_minute_row(timestamp="2026-05-13 14:30:00", interval="5m")],
            "真实旧缓存",
        )
        stored_at = (market_now_naive() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE kline_daily SET fetched_at = ?", (stored_at,))
            conn.execute("UPDATE kline_minute SET fetched_at = ?", (stored_at,))

        providers = {
            "stale_one": StaleProvider("旧源一", "2026-05-12", "2026-05-13 14:30:00"),
            "stale_two": StaleProvider("旧源二", "2026-05-09", "2026-05-13 14:20:00"),
        }
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers=providers,
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "stale_one"), (2, "stale_two")],
            now=lambda: current,
        )

        daily = await coordinator.kline("600519.SH", limit=20, use_cache=False)
        minute = await coordinator.minute_kline("600519.SH", interval="5m", limit=20, use_cache=False)
        with sqlite3.connect(path) as conn:
            daily_db = conn.execute("SELECT date, close, source, fetched_at FROM kline_daily ORDER BY date").fetchall()
            minute_db = conn.execute("SELECT timestamp, close, source, fetched_at FROM kline_minute ORDER BY timestamp").fetchall()
        statuses = [(item.name, item.kind, item.success_count, item.failure_count) for item in cache.provider_capability_statuses() if item.name in providers]
        return daily, minute, daily_db, minute_db, statuses, stored_at

    with TemporaryDirectory() as tmpdir:
        daily, minute, daily_db, minute_db, statuses, stored_at = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [(item.source, item.from_cache, item.fallback_used) for item in daily] == [("真实旧缓存", True, True)]
    assert [(item.source, item.from_cache, item.fallback_used) for item in minute] == [("真实旧缓存", True, True)]
    assert daily_db == [("2026-05-12", 100.0, "真实旧缓存", stored_at)]
    assert minute_db == [("2026-05-13 14:30:00", 101.0, "真实旧缓存", stored_at)]
    assert sorted(statuses) == [
        ("stale_one", "kline", 1, 0),
        ("stale_one", "minute", 0, 1),
        ("stale_two", "kline", 1, 0),
        ("stale_two", "minute", 0, 1),
    ]


def test_all_stale_kline_sources_without_cache_raise_and_leave_tables_empty() -> None:
    current = datetime(2026, 5, 13, 16, 0, 0)

    class StaleProvider:
        def __init__(self, source_name: str, daily_date: str, minute_timestamp: str) -> None:
            self.source_name = source_name
            self.daily_date = daily_date
            self.minute_timestamp = minute_timestamp

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date=self.daily_date)]

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            return [_minute_row(timestamp=self.minute_timestamp, interval=interval)]

    async def run_check(path: Path):
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        providers = {
            "stale_one": StaleProvider("旧源一", "2026-05-12", "2026-05-13 14:30:00"),
            "stale_two": StaleProvider("旧源二", "2026-05-09", "2026-05-13 14:20:00"),
        }
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers=providers,
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "stale_one"), (2, "stale_two")],
            now=lambda: current,
        )

        with pytest.raises(RuntimeError) as daily_error:
            await coordinator.kline("600519.SH", limit=20, use_cache=False)
        with pytest.raises(RuntimeError) as minute_error:
            await coordinator.minute_kline("600519.SH", interval="5m", limit=20, use_cache=False)

        with sqlite3.connect(path) as conn:
            daily_count = conn.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0]
            minute_count = conn.execute("SELECT COUNT(*) FROM kline_minute").fetchone()[0]
        statuses = [(item.name, item.kind, item.success_count, item.failure_count) for item in cache.provider_capability_statuses() if item.name in providers]
        return str(daily_error.value), str(minute_error.value), daily_count, minute_count, statuses

    with TemporaryDirectory() as tmpdir:
        daily_error, minute_error, daily_count, minute_count, statuses = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert daily_error == (
        "所有K线数据源均不可用：" "stale_one: 旧源一 日K业务时间无效或已过期：2026-05-12；" "stale_two: 旧源二 日K业务时间无效或已过期：2026-05-09"
    )
    assert minute_error == (
        "所有分钟K线数据源均不可用："
        "stale_one: 旧源一 分钟K线业务时间无效或已过期：2026-05-13 14:30:00；"
        "stale_two: 旧源二 分钟K线业务时间无效或已过期：2026-05-13 14:20:00"
    )
    assert daily_count == 0
    assert minute_count == 0
    assert sorted(statuses) == [
        ("stale_one", "kline", 1, 0),
        ("stale_one", "minute", 0, 1),
        ("stale_two", "kline", 1, 0),
        ("stale_two", "minute", 0, 1),
    ]


def test_minute_kline_fallback_cache_ignores_log_event_failure() -> None:
    class LogFailingSQLiteCache(SQLiteCache):
        def log_event(self, category: str, message: str) -> None:
            raise sqlite3.DatabaseError("event log readonly")

    class FailingMinuteProvider:
        source_name = "失败分钟线源"

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            raise RuntimeError("minute down")

    async def run_check(path: Path) -> list[MinuteKline]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = LogFailingSQLiteCache(path)
        cache.save_minute_klines(
            "600519.SH",
            "5m",
            [_minute_row(timestamp=f"2026-05-13 10:{index:02d}:00", interval="5m") for index in range(12)],
            "历史分钟缓存",
        )
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"failing": FailingMinuteProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "failing")],
        )

        return await coordinator.minute_kline("600519.SH", interval="5m", limit=12, use_cache=False)

    with TemporaryDirectory() as tmpdir:
        rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert len(rows) == 12
    assert all(item.from_cache and item.fallback_used for item in rows)


def test_daily_kline_returns_provider_rows_when_cache_write_fails() -> None:
    class CacheWriteFailingSQLiteCache(SQLiteCache):
        def save_klines(self, symbol: str, klines: list[Kline], source: str) -> None:
            raise sqlite3.DatabaseError("kline cache readonly")

    class LiveKlineProvider:
        source_name = "实时K线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date="2026-05-13", source=self.source_name)]

    async def run_check(path: Path) -> tuple[list[Kline], bool, int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = CacheWriteFailingSQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"live": LiveKlineProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "live")],
        )

        rows = await coordinator.kline("600519.SH", limit=20, use_cache=False)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "live" and item.kind == "kline")
        return rows, runtime.is_cooling("live", "kline"), status.failure_count

    with TemporaryDirectory() as tmpdir:
        rows, cooling, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["实时K线源"]
    assert cooling is False
    assert failure_count == 0


def test_kline_coordinator_offloads_daily_and_minute_cache_io() -> None:
    class ThreadTrackingKlineCache(SQLiteCache):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.io_threads: dict[str, set[int]] = {}

        def _track(self, operation: str) -> None:
            self.io_threads.setdefault(operation, set()).add(threading.get_ident())

        def get_klines(self, symbol: str, limit: int, max_age_seconds: int) -> list[Kline]:
            self._track("get_klines")
            return super().get_klines(symbol, limit, max_age_seconds)

        def save_klines(self, symbol: str, klines: list[Kline], source: str) -> None:
            self._track("save_klines")
            super().save_klines(symbol, klines, source)

        def get_minute_klines(
            self,
            symbol: str,
            interval: str,
            limit: int,
            max_age_seconds: int,
        ) -> list[MinuteKline]:
            self._track("get_minute_klines")
            return super().get_minute_klines(symbol, interval, limit, max_age_seconds)

        def save_minute_klines(
            self,
            symbol: str,
            interval: str,
            rows: list[MinuteKline],
            source: str,
        ) -> None:
            self._track("save_minute_klines")
            super().save_minute_klines(symbol, interval, rows, source)

        def update_provider_capability_success(
            self,
            name: str,
            kind: str,
            priority: int,
            latency_ms: float,
        ) -> None:
            self._track("provider_success")
            super().update_provider_capability_success(name, kind, priority, latency_ms)

    class LiveKlineProvider:
        source_name = "实时K线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date="2026-05-13")]

        async def minute_kline(
            self,
            symbol: str,
            interval: str = "5m",
            limit: int = 120,
        ) -> list[MinuteKline]:
            return [_minute_row(timestamp="2026-05-13 10:15:00", interval=interval)]

    async def run_check(path: Path) -> tuple[list[str], dict[str, set[int]], int]:
        settings = Settings()
        cache = ThreadTrackingKlineCache(path)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"live": LiveKlineProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "live")],
        )
        event_loop_thread = threading.get_ident()

        daily = await coordinator.kline("600519.SH", limit=20, use_cache=True)
        minute = await coordinator.minute_kline("600519.SH", interval="5m", limit=20, use_cache=True)
        return [daily[0].source or "", minute[0].source or ""], cache.io_threads, event_loop_thread

    with TemporaryDirectory() as tmpdir:
        sources, io_threads, event_loop_thread = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert sources == ["实时K线源", "实时K线源"]
    assert {
        "get_klines",
        "save_klines",
        "get_minute_klines",
        "save_minute_klines",
        "provider_success",
    } <= io_threads.keys()
    assert all(event_loop_thread not in thread_ids for thread_ids in io_threads.values())


def test_short_cross_source_daily_refresh_does_not_splice_longer_history() -> None:
    end_date = KLINE_TEST_NOW.date()
    original = [
        make_kline(
            date=(end_date - timedelta(days=259 - index)).isoformat(),
            source="长历史缓存",
        )
        for index in range(260)
    ]
    refresh = [item.model_copy(update={"source": None}) for item in original[-120:]]
    refresh[-1] = make_kline(date=end_date.isoformat(), close=111.0)

    class ShortRefreshProvider:
        source_name = "调度刷新源"

        def __init__(self) -> None:
            self.calls: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls.append(limit)
            return refresh

    async def run_check(path: Path) -> tuple[list[Kline], list[Kline], list[int]]:
        settings = Settings()
        cache = SQLiteCache(path)
        cache.save_klines("600519.SH", original, "长历史缓存")
        provider = ShortRefreshProvider()
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"short": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "short")],
        )

        fetched = await coordinator.kline("600519.SH", limit=120, use_cache=False)
        stored = cache.get_klines("600519.SH", limit=300, max_age_seconds=10**9)
        return fetched, stored, provider.calls

    with TemporaryDirectory() as tmpdir:
        fetched, stored, calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert len(fetched) == 120
    assert calls == [260]
    assert len(stored) == 260
    assert stored[0].date == original[0].date
    assert stored[-1].close == 100.0
    fetched_contracts = {(item.adjustment_mode, item.as_of, item.data_version, item.contract_version) for item in fetched}
    stored_contracts = {(item.adjustment_mode, item.as_of, item.data_version, item.contract_version) for item in stored}
    assert len(fetched_contracts) == 1
    assert fetched_contracts == stored_contracts
    assert {item.source for item in fetched} == {"调度刷新源"}
    assert {item.source for item in stored} == {"长历史缓存"}


def test_short_new_daily_data_version_does_not_replace_longer_stored_vintage() -> None:
    end_date = KLINE_TEST_NOW.date()
    original = [
        make_kline(
            date=(end_date - timedelta(days=259 - index)).isoformat(),
            data_version="daily-version-1",
        )
        for index in range(260)
    ]
    replacement = [
        make_kline(
            date=(end_date - timedelta(days=119 - index)).isoformat(),
            data_version="daily-version-2",
        )
        for index in range(120)
    ]

    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        cache.save_klines("600519.SH", original, "版本一")
        cache.save_klines("600519.SH", replacement, "版本二")
        stored = cache.get_klines("600519.SH", limit=300, max_age_seconds=10**9)

    assert len(stored) == 260
    assert stored[0].date == original[0].date
    assert {item.data_version for item in stored} == {"daily-version-1"}
    assert len({(item.adjustment_mode, item.as_of, item.data_version, item.contract_version) for item in stored}) == 1


def test_equal_length_older_daily_vintages_cannot_replace_newer_cache() -> None:
    end_date = KLINE_TEST_NOW.date()

    def rows(*, as_of: date, data_version: str, close: float) -> list[Kline]:
        return [
            make_kline(
                date=(as_of - timedelta(days=19 - index)).isoformat(),
                as_of=as_of.isoformat(),
                data_version=data_version,
                close=close,
            )
            for index in range(20)
        ]

    newest = [
        item.model_copy(update={"fetched_at": "2026-05-13 10:00:03.000000"})
        for item in rows(as_of=end_date, data_version="daily-version-10", close=110.0)
    ]
    older_as_of = [
        item.model_copy(update={"fetched_at": "2026-05-13 10:00:04.000000"})
        for item in rows(
            as_of=end_date - timedelta(days=1),
            data_version="daily-version-99",
            close=90.0,
        )
    ]
    older_version = [
        item.model_copy(update={"fetched_at": "2026-05-13 10:00:02.000000"})
        for item in rows(as_of=end_date, data_version="daily-version-2", close=80.0)
    ]

    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        cache.save_klines("600519.SH", newest, "最新版本")
        cache.save_klines("600519.SH", older_as_of, "旧日期版本")
        cache.save_klines("600519.SH", older_version, "同日旧版本")
        stored = cache.get_klines("600519.SH", limit=30, max_age_seconds=10**9)

    assert len(stored) == 20
    assert {item.as_of for item in stored} == {end_date.isoformat()}
    assert {item.data_version for item in stored} == {"daily-version-10"}
    assert {item.close for item in stored} == {110.0}


@pytest.mark.parametrize(
    ("primary_delay", "fallback_delay"),
    ((0.0, 0.03), (0.03, 0.0)),
)
def test_same_as_of_concurrent_writes_always_keep_non_fallback_quality(
    primary_delay: float,
    fallback_delay: float,
) -> None:
    as_of = KLINE_TEST_NOW.date()

    def rows(
        *,
        source: str,
        data_version: str,
        close: float,
        fetched_at: str,
        fallback_used: bool,
    ) -> list[Kline]:
        return [
            make_kline(
                date=(as_of - timedelta(days=19 - index)).isoformat(),
                as_of=as_of.isoformat(),
                data_version=data_version,
                close=close,
                source=source,
                fallback_used=fallback_used,
            ).model_copy(update={"fetched_at": fetched_at})
            for index in range(20)
        ]

    primary = rows(
        source="AAA主源",
        data_version="daily-kline.v1|qfq|AAA主源|2026-05-13",
        close=110.0,
        fetched_at="2026-05-13 10:00:00.000001",
        fallback_used=False,
    )
    fallback = rows(
        source="ZZZ后备源",
        data_version="daily-kline.v1|qfq|ZZZ后备源|2026-05-13",
        close=90.0,
        fetched_at="2026-05-13 11:00:00.000001",
        fallback_used=True,
    )

    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        barrier = threading.Barrier(3)
        errors: list[Exception] = []

        def save(candidate: list[Kline], source: str, delay: float) -> None:
            try:
                barrier.wait(timeout=5)
                time.sleep(delay)
                cache.save_klines("600519.SH", candidate, source)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=save, args=(primary, "AAA主源", primary_delay)),
            threading.Thread(target=save, args=(fallback, "ZZZ后备源", fallback_delay)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)
        stored = cache.get_klines("600519.SH", limit=30, max_age_seconds=10**9)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert {item.source for item in stored} == {"AAA主源"}
    assert {item.close for item in stored} == {110.0}
    assert all(not item.fallback_used for item in stored)


@pytest.mark.parametrize("newer_first", (False, True))
def test_same_quality_daily_writes_use_fetched_at_not_provider_text(
    newer_first: bool,
) -> None:
    as_of = KLINE_TEST_NOW.date()

    def rows(source: str, data_version: str, close: float, fetched_at: str) -> list[Kline]:
        return [
            make_kline(
                date=(as_of - timedelta(days=9 - index)).isoformat(),
                as_of=as_of.isoformat(),
                data_version=data_version,
                close=close,
                source=source,
            ).model_copy(update={"fetched_at": fetched_at})
            for index in range(10)
        ]

    older = rows(
        "ZZZ较早抓取源",
        "daily-kline.v1|qfq|ZZZ较早抓取源|2026-05-13",
        90.0,
        "2026-05-13 10:00:00.000001",
    )
    newer = rows(
        "AAA较新抓取源",
        "daily-kline.v1|qfq|AAA较新抓取源|2026-05-13",
        110.0,
        "2026-05-13T02:00:00.000002Z",
    )

    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        ordered = (newer, older) if newer_first else (older, newer)
        for candidate in ordered:
            cache.save_klines("600519.SH", candidate, candidate[0].source or "unknown")
        stored = cache.get_klines("600519.SH", limit=20, max_age_seconds=10**9)

    assert {item.source for item in stored} == {"AAA较新抓取源"}
    assert {item.close for item in stored} == {110.0}


def test_short_new_vintage_provider_does_not_shrink_longer_cache() -> None:
    end_date = KLINE_TEST_NOW.date()
    old_end = end_date - timedelta(days=1)
    original = [
        make_kline(
            date=(old_end - timedelta(days=259 - index)).isoformat(),
            as_of="2026-05-12",
            data_version="daily-version-1",
            source="旧版本日线源",
        )
        for index in range(260)
    ]
    new_vintage = [
        make_kline(
            date=(end_date - timedelta(days=119 - index)).isoformat(),
            as_of="2026-05-13",
            data_version="daily-version-2",
        )
        for index in range(120)
    ]

    class ShortNewVintageProvider:
        source_name = "新版本短日线源"

        def __init__(self) -> None:
            self.calls: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls.append(limit)
            return new_vintage

    async def run_check(
        path: Path,
    ) -> tuple[list[Kline], list[Kline], list[Kline], list[int]]:
        settings = Settings()
        cache = SQLiteCache(path)
        cache.save_klines("600519.SH", original, "旧版本日线源")
        provider = ShortNewVintageProvider()
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"new": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "new")],
        )

        fetched = await coordinator.kline("600519.SH", limit=120, use_cache=False)
        cached = await coordinator.kline("600519.SH", limit=260, use_cache=True)
        stored = cache.get_klines("600519.SH", limit=300, max_age_seconds=10**9)
        return fetched, cached, stored, provider.calls

    with TemporaryDirectory() as tmpdir:
        fetched, cached, stored, calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == [260]
    assert len(fetched) == 120
    assert {item.data_version for item in fetched} == {"daily-version-2"}
    assert len(cached) == len(stored) == 260
    assert all(item.from_cache for item in cached)
    assert {item.data_version for item in cached} == {"daily-version-1"}
    assert {item.data_version for item in stored} == {"daily-version-1"}


def test_concurrent_short_new_vintage_cannot_overwrite_complete_replacement() -> None:
    end_date = KLINE_TEST_NOW.date()
    original = [
        make_kline(
            date=(end_date - timedelta(days=259 - index)).isoformat(),
            data_version="daily-version-1",
        )
        for index in range(260)
    ]
    complete_replacement = [
        make_kline(
            date=(end_date - timedelta(days=259 - index)).isoformat(),
            data_version="daily-version-2",
        )
        for index in range(260)
    ]
    short_newer_vintage = [
        make_kline(
            date=(end_date - timedelta(days=119 - index)).isoformat(),
            data_version="daily-version-3",
        )
        for index in range(120)
    ]

    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        cache.save_klines("600519.SH", original, "版本一")
        barrier = threading.Barrier(3)
        errors: list[Exception] = []

        def save(rows: list[Kline], source: str) -> None:
            try:
                barrier.wait(timeout=5)
                cache.save_klines("600519.SH", rows, source)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(
                target=save,
                args=(complete_replacement, "完整版本二"),
            ),
            threading.Thread(
                target=save,
                args=(short_newer_vintage, "短版本三"),
            ),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        stored = cache.get_klines("600519.SH", limit=300, max_age_seconds=10**9)

    assert len(stored) == 260
    assert {item.data_version for item in stored} == {"daily-version-2"}
    assert len({(item.adjustment_mode, item.as_of, item.data_version, item.contract_version) for item in stored}) == 1


def test_concurrent_equal_length_daily_vintages_always_keep_newest() -> None:
    end_date = KLINE_TEST_NOW.date()

    def rows(*, as_of: date, data_version: str, close: float) -> list[Kline]:
        return [
            make_kline(
                date=(as_of - timedelta(days=39 - index)).isoformat(),
                as_of=as_of.isoformat(),
                data_version=data_version,
                close=close,
            )
            for index in range(40)
        ]

    original = rows(as_of=end_date - timedelta(days=2), data_version="daily-version-1", close=100.0)
    older = rows(as_of=end_date - timedelta(days=1), data_version="daily-version-2", close=102.0)
    newest = rows(as_of=end_date, data_version="daily-version-3", close=103.0)

    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        cache.save_klines("600519.SH", original, "初始版本")
        barrier = threading.Barrier(3)
        errors: list[Exception] = []

        def save(candidate: list[Kline], source: str) -> None:
            try:
                barrier.wait(timeout=5)
                cache.save_klines("600519.SH", candidate, source)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=save, args=(older, "旧并发版本")),
            threading.Thread(target=save, args=(newest, "新并发版本")),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        stored = cache.get_klines("600519.SH", limit=50, max_age_seconds=10**9)

    assert len(stored) == 40
    assert {item.as_of for item in stored} == {end_date.isoformat()}
    assert {item.data_version for item in stored} == {"daily-version-3"}
    assert {item.close for item in stored} == {103.0}


def test_refresh_requests_full_existing_coverage_and_replaces_with_one_new_vintage() -> None:
    end_date = KLINE_TEST_NOW.date()
    old_end = end_date - timedelta(days=1)
    original = [
        make_kline(
            date=(old_end - timedelta(days=259 - index)).isoformat(),
            as_of="2026-05-12",
            data_version="daily-version-1",
        )
        for index in range(260)
    ]
    new_vintage = [
        make_kline(
            date=(end_date - timedelta(days=259 - index)).isoformat(),
            as_of="2026-05-13",
            data_version="daily-version-2",
        )
        for index in range(260)
    ]

    class NewVintageProvider:
        source_name = "新版本日线源"

        def __init__(self) -> None:
            self.calls: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls.append(limit)
            return new_vintage[-limit:]

    async def run_check(path: Path) -> tuple[list[Kline], list[Kline], list[int]]:
        settings = Settings()
        cache = SQLiteCache(path)
        cache.save_klines("600519.SH", original, "旧版本日线源")
        provider = NewVintageProvider()
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"new": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "new")],
        )

        returned = await coordinator.kline("600519.SH", limit=120, use_cache=False)
        stored = cache.get_klines("600519.SH", limit=300, max_age_seconds=10**9)
        return returned, stored, provider.calls

    with TemporaryDirectory() as tmpdir:
        returned, stored, calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    returned_contracts = {(item.adjustment_mode, item.as_of, item.data_version, item.contract_version) for item in returned}
    stored_contracts = {(item.adjustment_mode, item.as_of, item.data_version, item.contract_version) for item in stored}
    assert calls == [260]
    assert len(returned) == 120
    assert len(stored) == 260
    assert returned_contracts == {("qfq", "2026-05-13", "daily-version-2", "daily-kline.v1")}
    assert stored_contracts == returned_contracts


def test_stale_complete_cache_refreshes_with_verified_incremental_tail() -> None:
    old_end = KLINE_TEST_NOW.date() - timedelta(days=1)
    original = [
        make_kline(
            date=(old_end - timedelta(days=259 - index)).isoformat(),
            as_of=old_end.isoformat(),
            data_version="daily-version-1",
            source="增量日线源",
            fallback_used=True,
        )
        for index in range(260)
    ]
    refresh = [
        item.model_copy(
            update={
                "as_of": KLINE_TEST_NOW.date().isoformat(),
                "data_version": "daily-version-2",
                "source": None,
                "fallback_used": False,
            }
        )
        for item in original[-39:]
    ]
    refresh.append(
        make_kline(
            date=KLINE_TEST_NOW.date().isoformat(),
            as_of=KLINE_TEST_NOW.date().isoformat(),
            data_version="daily-version-2",
        )
    )

    class IncrementalProvider:
        source_name = "增量日线源"

        def __init__(self) -> None:
            self.calls: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls.append(limit)
            return refresh[-limit:]

    async def run_check(path: Path) -> tuple[list[Kline], list[Kline], list[int]]:
        settings = Settings(kline_cache_seconds=1)
        cache = SQLiteCache(path)
        cache.save_klines("600519.SH", original, "增量日线源")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE kline_daily SET fetched_at = '2020-01-01 00:00:00'")
        provider = IncrementalProvider()
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"incremental": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "incremental")],
        )

        returned = await coordinator.kline("600519.SH", limit=260, use_cache=True)
        stored = cache.get_klines("600519.SH", limit=300, max_age_seconds=10**9)
        return returned, stored, provider.calls

    with TemporaryDirectory() as tmpdir:
        returned, stored, calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == [40]
    assert len(returned) == len(stored) == 260
    assert returned[-1].date == KLINE_TEST_NOW.date().isoformat()
    assert {item.data_version for item in returned} == {
        "daily-version-1",
        "daily-version-2",
    }
    assert {item.data_version for item in stored} == {
        "daily-version-1",
        "daily-version-2",
    }
    assert {item.data_version for item in returned[:220]} == {"daily-version-1"}
    assert {item.data_version for item in returned[220:]} == {"daily-version-2"}
    assert {item.source for item in returned} == {"增量日线源"}
    assert sum(item.fallback_used for item in returned) == 220
    assert sum(item.fallback_used for item in stored) == 220
    assert all(item.fallback_used for item in returned[:220])
    assert all(not item.fallback_used for item in returned[220:])


@pytest.mark.parametrize("mismatch", ("source", "volume"))
def test_incremental_source_or_volume_mismatch_triggers_full_refresh(
    mismatch: str,
) -> None:
    old_end = KLINE_TEST_NOW.date() - timedelta(days=1)
    original_source = "原始日线源"
    provider_source = "切换日线源" if mismatch == "source" else original_source
    original = [
        make_kline(
            date=(old_end - timedelta(days=79 - index)).isoformat(),
            close=100 + index / 100,
            volume=10_000 + index,
            as_of=old_end.isoformat(),
            data_version="daily-version-1",
            source=original_source,
        )
        for index in range(80)
    ]
    refresh = [
        item.model_copy(
            update={
                "as_of": KLINE_TEST_NOW.date().isoformat(),
                "data_version": "daily-version-2",
                "source": None,
            }
        )
        for item in original[-39:]
    ]
    changed_date = refresh[5].date
    if mismatch == "volume":
        refresh[5] = refresh[5].model_copy(update={"volume": refresh[5].volume + 500})
    refresh.append(
        make_kline(
            date=KLINE_TEST_NOW.date().isoformat(),
            close=101.0,
            volume=20_000,
            as_of=KLINE_TEST_NOW.date().isoformat(),
            data_version="daily-version-2",
        )
    )
    full_history = [
        item.model_copy(
            update={
                "as_of": KLINE_TEST_NOW.date().isoformat(),
                "data_version": "daily-version-2",
                "source": None,
                "volume": item.volume + 500
                if mismatch == "volume" and item.date == changed_date
                else item.volume,
            }
        )
        for item in original[1:]
    ]
    full_history.append(refresh[-1])

    class RefreshProvider:
        source_name = provider_source

        def __init__(self) -> None:
            self.calls: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls.append(limit)
            return refresh if limit == 40 else full_history[-limit:]

    async def run_check(path: Path) -> tuple[list[Kline], list[Kline], list[int]]:
        settings = Settings(kline_cache_seconds=1)
        cache = SQLiteCache(path)
        cache.save_klines("600519.SH", original, original_source)
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE kline_daily SET fetched_at = '2020-01-01 00:00:00'")
        provider = RefreshProvider()
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"refresh": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "refresh")],
        )
        returned = await coordinator.kline("600519.SH", limit=80, use_cache=True)
        stored = cache.get_klines("600519.SH", limit=100, max_age_seconds=10**9)
        return returned, stored, provider.calls

    with TemporaryDirectory() as tmpdir:
        returned, stored, calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == [40, 80]
    assert len(returned) == len(stored) == 80
    assert {item.data_version for item in returned} == {"daily-version-2"}
    assert {item.source for item in stored} == {provider_source}


def test_incremental_overlap_change_falls_back_to_full_history_refresh() -> None:
    old_end = KLINE_TEST_NOW.date() - timedelta(days=1)
    original = [
        make_kline(
            date=(old_end - timedelta(days=259 - index)).isoformat(),
            close=100 + index / 100,
            as_of=old_end.isoformat(),
            data_version="daily-version-1",
            source="除权重算日线源",
        )
        for index in range(260)
    ]
    replacement = [
        make_kline(
            date=(KLINE_TEST_NOW.date() - timedelta(days=259 - index)).isoformat(),
            close=90 + index / 100,
            as_of=KLINE_TEST_NOW.date().isoformat(),
            data_version="daily-version-2",
        )
        for index in range(260)
    ]

    class RebasedProvider:
        source_name = "除权重算日线源"

        def __init__(self) -> None:
            self.calls: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls.append(limit)
            return replacement[-limit:]

    async def run_check(path: Path) -> tuple[list[Kline], list[Kline], list[int]]:
        settings = Settings(kline_cache_seconds=1)
        cache = SQLiteCache(path)
        cache.save_klines("600519.SH", original, "除权重算日线源")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE kline_daily SET fetched_at = '2020-01-01 00:00:00'")
        provider = RebasedProvider()
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"rebased": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "rebased")],
        )

        returned = await coordinator.kline("600519.SH", limit=260, use_cache=True)
        stored = cache.get_klines("600519.SH", limit=300, max_age_seconds=10**9)
        return returned, stored, provider.calls

    with TemporaryDirectory() as tmpdir:
        returned, stored, calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == [40, 260]
    assert len(returned) == len(stored) == 260
    assert returned[0].close == replacement[0].close
    assert {item.data_version for item in stored} == {"daily-version-2"}


def test_insufficient_daily_cache_coverage_fetches_requested_history() -> None:
    end_date = KLINE_TEST_NOW.date()
    complete_history = [make_kline(date=(end_date - timedelta(days=239 - index)).isoformat()) for index in range(240)]

    class CompleteHistoryProvider:
        source_name = "完整历史源"

        def __init__(self) -> None:
            self.calls: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls.append(limit)
            return complete_history[-limit:]

    async def run_check(path: Path) -> tuple[list[Kline], list[Kline], list[int]]:
        settings = Settings()
        cache = SQLiteCache(path)
        cache.save_klines("600519.SH", complete_history[-120:], "半量日线缓存")
        provider = CompleteHistoryProvider()
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"complete": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "complete")],
        )

        rows = await coordinator.kline("600519.SH", limit=240, use_cache=True)
        stored = cache.get_klines("600519.SH", limit=300, max_age_seconds=10**9)
        return rows, stored, provider.calls

    with TemporaryDirectory() as tmpdir:
        rows, stored, calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == [240]
    assert len(rows) == 240
    assert len(stored) == 240
    assert {item.source for item in rows} == {"完整历史源"}


def test_short_provider_history_marks_exhaustion_for_later_cache_reuse() -> None:
    end_date = KLINE_TEST_NOW.date()
    available_history = [make_kline(date=(end_date - timedelta(days=19 - index)).isoformat()) for index in range(20)]

    class ExhaustedHistoryProvider:
        source_name = "短历史源"

        def __init__(self) -> None:
            self.calls: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls.append(limit)
            return available_history

    async def run_check(path: Path) -> tuple[list[Kline], list[Kline], list[int]]:
        settings = Settings()
        cache = SQLiteCache(path)
        provider = ExhaustedHistoryProvider()
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"short": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "short")],
        )

        fetched = await coordinator.kline("600519.SH", limit=40, use_cache=False)
        cached = await coordinator.kline("600519.SH", limit=40, use_cache=True)
        return fetched, cached, provider.calls

    with TemporaryDirectory() as tmpdir:
        fetched, cached, calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == [40]
    assert len(fetched) == len(cached) == 20
    assert all(not item.from_cache for item in fetched)
    assert all(item.from_cache for item in cached)


def test_daily_provider_chain_continues_from_short_primary_to_complete_backup() -> None:
    end_date = KLINE_TEST_NOW.date()
    complete_history = [make_kline(date=(end_date - timedelta(days=39 - index)).isoformat()) for index in range(40)]

    class ShortPrimaryProvider:
        source_name = "优先短历史源"

        def __init__(self) -> None:
            self.calls: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls.append(limit)
            return complete_history[-20:]

    class CompleteBackupProvider:
        source_name = "完整备用源"

        def __init__(self) -> None:
            self.calls: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls.append(limit)
            return complete_history[-limit:]

    async def run_check(
        path: Path,
    ) -> tuple[list[Kline], list[Kline], list[int], list[int], dict[str, tuple[int, int]], list[bool]]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        primary = ShortPrimaryProvider()
        backup = CompleteBackupProvider()
        runtime = ProviderRuntime(cache, settings)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"primary": primary, "backup": backup},
            runtime=runtime,
            priority=lambda kind: [(1, "primary"), (2, "backup")],
        )

        rows = await coordinator.kline("600519.SH", limit=40, use_cache=False)
        stored = cache.get_klines("600519.SH", limit=60, max_age_seconds=10**9)
        statuses = {item.name: (item.success_count, item.failure_count) for item in cache.provider_capability_statuses() if item.kind == "kline"}
        cooling = [
            runtime.is_cooling("primary", "kline"),
            runtime.is_cooling("backup", "kline"),
        ]
        return rows, stored, primary.calls, backup.calls, statuses, cooling

    with TemporaryDirectory() as tmpdir:
        rows, stored, primary_calls, backup_calls, statuses, cooling = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert primary_calls == backup_calls == [40]
    assert len(rows) == len(stored) == 40
    assert {item.source for item in rows} == {"完整备用源"}
    assert {item.source for item in stored} == {"完整备用源"}
    assert statuses == {"primary": (1, 0), "backup": (1, 0)}
    assert cooling == [False, False]


def test_all_short_daily_providers_choose_longest_and_larger_request_retries() -> None:
    end_date = KLINE_TEST_NOW.date()
    available_history = [make_kline(date=(end_date - timedelta(days=29 - index)).isoformat()) for index in range(30)]

    class ShortProvider:
        def __init__(self, source_name: str, row_count: int) -> None:
            self.source_name = source_name
            self.row_count = row_count
            self.calls: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.calls.append(limit)
            return available_history[-self.row_count :]

    async def run_check(
        path: Path,
    ) -> tuple[
        list[Kline],
        list[Kline],
        list[Kline],
        list[int],
        list[int],
        dict[str, tuple[int, int]],
    ]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        primary = ShortProvider("优先短历史源", 20)
        backup = ShortProvider("较长备用源", 30)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"primary": primary, "backup": backup},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "primary"), (2, "backup")],
        )

        fetched = await coordinator.kline("600519.SH", limit=40, use_cache=False)
        cached = await coordinator.kline("600519.SH", limit=40, use_cache=True)
        larger = await coordinator.kline("600519.SH", limit=60, use_cache=True)
        statuses = {item.name: (item.success_count, item.failure_count) for item in cache.provider_capability_statuses() if item.kind == "kline"}
        return fetched, cached, larger, primary.calls, backup.calls, statuses

    with TemporaryDirectory() as tmpdir:
        fetched, cached, larger, primary_calls, backup_calls, statuses = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert primary_calls == backup_calls == [40, 60]
    assert len(fetched) == len(cached) == len(larger) == 30
    assert {item.source for item in fetched} == {"较长备用源"}
    assert all(item.from_cache for item in cached)
    assert {item.source for item in larger} == {"较长备用源"}
    assert all(not item.from_cache for item in larger)
    assert statuses == {"primary": (2, 0), "backup": (2, 0)}


def test_insufficient_daily_history_refreshes_while_early_minute_cache_skips_provider() -> None:
    class TrackingKlineProvider:
        source_name = "实时K线源"

        def __init__(self, latest_date: str) -> None:
            self.latest_date = datetime.fromisoformat(latest_date).date()
            self.daily_limits: list[int] = []
            self.minute_limits: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.daily_limits.append(limit)
            return [make_kline(date=(self.latest_date - timedelta(days=limit - index - 1)).isoformat()) for index in range(limit)]

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            self.minute_limits.append(limit)
            return [_minute_row(timestamp=f"2026-05-13 10:{index:02d}:00", interval=interval) for index in range(limit)]

    async def run_check(path: Path) -> tuple[int, int, list[int], list[int], str, str]:
        settings = Settings()
        cache = SQLiteCache(path)
        current = datetime(2026, 5, 13, 10, 0, 0)
        latest = current.date()
        cache.save_klines(
            "600519.SH",
            [make_kline(date=(latest - timedelta(days=19 - index)).isoformat(), source="半量日线缓存") for index in range(20)],
            "半量日线缓存",
        )
        cache.save_minute_klines(
            "600519.SH",
            "5m",
            [_minute_row(timestamp=f"2026-05-13 09:{40 + index:02d}:00", interval="5m") for index in range(12)],
            "半量分钟缓存",
        )
        provider = TrackingKlineProvider(latest.isoformat())
        runtime = ProviderRuntime(cache, settings)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"live": provider},
            runtime=runtime,
            priority=lambda kind: [(1, "live")],
            now=lambda: current,
        )

        daily = await coordinator.kline("600519.SH", limit=40, use_cache=True)
        minute = await coordinator.minute_kline("600519.SH", interval="5m", limit=20, use_cache=True)
        return len(daily), len(minute), provider.daily_limits, provider.minute_limits, daily[0].source or "", minute[0].source or ""

    with TemporaryDirectory() as tmpdir:
        daily_len, minute_len, daily_limits, minute_limits, daily_source, minute_source = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert daily_len == 40
    assert minute_len == 12
    assert daily_limits == [40]
    assert minute_limits == []
    assert daily_source == "实时K线源"
    assert minute_source == "半量分钟缓存"


def test_minute_kline_stale_business_timestamp_does_not_skip_provider() -> None:
    class LiveMinuteProvider:
        source_name = "实时分钟线源"

        def __init__(self) -> None:
            self.calls: list[int] = []

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            self.calls.append(limit)
            return [_minute_row(timestamp=f"2026-07-08 10:{index:02d}:00", interval=interval) for index in range(limit)]

    async def run_check(path: Path) -> tuple[list[str], list[int]]:
        settings = Settings(minute_kline_cache_seconds=60 * 60)
        cache = SQLiteCache(path)
        cache.save_minute_klines(
            "600519.SH",
            "5m",
            [_minute_row(timestamp=f"2000-01-03 10:{index:02d}:00", interval="5m") for index in range(20)],
            "旧业务时间分钟缓存",
        )
        provider = LiveMinuteProvider()
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"live": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "live")],
            now=lambda: datetime(2026, 7, 8, 10, 20, 0),
        )

        rows = await coordinator.minute_kline("600519.SH", interval="5m", limit=20, use_cache=True)
        return [item.source or "" for item in rows], provider.calls

    with TemporaryDirectory() as tmpdir:
        sources, calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == [20]
    assert set(sources) == {"实时分钟线源"}


def test_unregistered_priority_provider_is_skipped_before_backup_without_status_noise() -> None:
    class BackupKlineProvider:
        source_name = "备用K线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date="2026-05-13")]

    async def run_check(path: Path) -> tuple[list[Kline], bool]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"backup": BackupKlineProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "missing"), (2, "backup")],
        )

        rows = await coordinator.kline("600519.SH", limit=20, use_cache=False)
        missing_status_exists = any(item.name == "missing" and item.kind == "kline" for item in cache.provider_capability_statuses())
        return rows, missing_status_exists

    with TemporaryDirectory() as tmpdir:
        rows, missing_status_exists = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["备用K线源"]
    assert rows[0].from_cache is False
    assert rows[0].fallback_used is True
    assert missing_status_exists is False


def test_prepare_daily_klines_classifies_all_invalid_rows_as_instrument_failure() -> None:
    invalid = make_kline(date="2026-05-13").model_copy(update={"high": math.inf})

    with pytest.raises(ProviderInstrumentDataError, match="坏K线源 日K没有有效记录：600519.SH"):
        _prepare_daily_klines(
            [invalid],
            "坏K线源",
            "600519.SH",
            20,
            KLINE_TEST_NOW,
        )


def test_invalid_provider_kline_rows_are_instrument_failure_before_backup() -> None:
    class InvalidKlineProvider:
        source_name = "坏K线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date="2026-05-13").model_copy(update={"high": math.inf})]

    class BackupKlineProvider:
        source_name = "备用K线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date="2026-05-13")]

    async def run_check(path: Path) -> tuple[list[Kline], bool, int, str | None]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"invalid": InvalidKlineProvider(), "backup": BackupKlineProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "invalid"), (2, "backup")],
        )

        rows = await coordinator.kline("600519.SH", limit=20, use_cache=False)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "invalid" and item.kind == "kline")
        return rows, runtime.is_cooling("invalid", "kline"), status.failure_count, status.last_error

    with TemporaryDirectory() as tmpdir:
        rows, cooling, failure_count, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["备用K线源"]
    assert rows[0].fallback_used is True
    assert cooling is False
    assert failure_count == 1
    assert last_error == "坏K线源 日K没有有效记录：600519.SH"


def test_kline_coordinator_filters_sorts_and_limits_provider_rows_before_save() -> None:
    class UnsortedProvider:
        source_name = "乱序源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [
                make_kline(date="not-a-date"),
                make_kline(date="2026-05-13"),
                make_kline(date="2026-05-11").model_copy(update={"high": math.inf}),
                make_kline(date="2026-05-12"),
            ]

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            return [
                _minute_row(timestamp="bad-time", interval=interval),
                _minute_row(timestamp="2026-05-13 10:15:00", interval=interval),
                _minute_row(timestamp="2026-05-13 10:05:00", interval=interval).model_copy(update={"amount": math.inf}),
                _minute_row(timestamp="2026-05-13 10:10:00", interval=interval),
            ]

    async def run_check(path: Path) -> tuple[list[Kline], list[Kline], list[MinuteKline], list[MinuteKline]]:
        settings = Settings()
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"unsorted": UnsortedProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "unsorted")],
        )

        daily_rows = await coordinator.kline("600519.SH", limit=2, use_cache=False)
        minute_rows = await coordinator.minute_kline("600519.SH", interval="5m", limit=2, use_cache=False)
        cached_daily = cache.get_klines("600519.SH", limit=10, max_age_seconds=10**9)
        cached_minute = cache.get_minute_klines("600519.SH", "5m", limit=10, max_age_seconds=10**9)
        return daily_rows, cached_daily, minute_rows, cached_minute

    with TemporaryDirectory() as tmpdir:
        daily_rows, cached_daily, minute_rows, cached_minute = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.date for item in daily_rows] == ["2026-05-12", "2026-05-13"]
    assert [item.date for item in cached_daily] == ["2026-05-12", "2026-05-13"]
    assert [item.source for item in daily_rows] == ["乱序源", "乱序源"]
    assert [item.timestamp for item in minute_rows] == ["2026-05-13 10:10:00", "2026-05-13 10:15:00"]
    assert [item.timestamp for item in cached_minute] == ["2026-05-13 10:10:00", "2026-05-13 10:15:00"]
    assert [item.source for item in minute_rows] == ["乱序源", "乱序源"]


def test_kline_coordinator_preserves_real_zero_and_optional_empty_values() -> None:
    class ZeroValueProvider:
        source_name = "零值源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date="2026-05-13", volume=0)]

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            return [
                _minute_row(timestamp="2026-05-13 10:10:00", interval=interval).model_copy(update={"volume": 0, "amount": None, "turnover_rate": None}),
                _minute_row(timestamp="2026-05-13 10:15:00", interval=interval).model_copy(update={"volume": 0, "amount": 0, "turnover_rate": 0}),
            ]

    async def run_check(path: Path) -> tuple[list[Kline], list[MinuteKline], list[Kline], list[MinuteKline]]:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"zero": ZeroValueProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "zero")],
        )

        daily = await coordinator.kline("600519.SH", limit=20, use_cache=False)
        minute = await coordinator.minute_kline("600519.SH", interval="5m", limit=20, use_cache=False)
        cached_daily = cache.get_klines("600519.SH", limit=20, max_age_seconds=10**9)
        cached_minute = cache.get_minute_klines("600519.SH", "5m", limit=20, max_age_seconds=10**9)
        return daily, minute, cached_daily, cached_minute

    with TemporaryDirectory() as tmpdir:
        daily, minute, cached_daily, cached_minute = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.volume for item in daily] == [0]
    assert [item.volume for item in cached_daily] == [0]
    assert [(item.volume, item.amount, item.turnover_rate) for item in minute] == [
        (0, None, None),
        (0, 0, 0),
    ]
    assert [(item.volume, item.amount, item.turnover_rate) for item in cached_minute] == [
        (0, None, None),
        (0, 0, 0),
    ]


def test_kline_coordinator_bounds_excessive_limits_before_provider_calls() -> None:
    class CapturingProvider:
        source_name = "限流源"

        def __init__(self) -> None:
            self.daily_limits: list[int] = []
            self.minute_limits: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.daily_limits.append(limit)
            return [make_kline(date="2026-05-13")]

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            self.minute_limits.append(limit)
            return [_minute_row(timestamp="2026-05-13 10:15:00", interval=interval)]

    async def run_check(path: Path) -> tuple[list[int], list[int]]:
        settings = Settings(max_daily_kline_rows=333, max_minute_kline_rows=3)
        cache = SQLiteCache(path)
        provider = CapturingProvider()
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"capturing": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "capturing")],
        )

        await coordinator.kline("600519.SH", limit=MAX_DAILY_KLINE_LIMIT + 1, use_cache=False)
        await coordinator.minute_kline("600519.SH", interval="5m", limit=10, use_cache=False)
        return provider.daily_limits, provider.minute_limits

    with TemporaryDirectory() as tmpdir:
        daily_limits, minute_limits = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert daily_limits == [333]
    assert minute_limits == [3]


@pytest.mark.parametrize("dirty_limit", [" ", float("nan"), float("inf"), -10, None])
def test_kline_limit_bounds_ignore_invalid_max_settings(dirty_limit) -> None:
    assert _bounded_limit(120, dirty_limit, DEFAULT_MAX_MINUTE_KLINE_LIMIT) == 120


def test_kline_coordinator_rejects_non_positive_limits_before_provider_calls() -> None:
    class ProviderShouldNotRun:
        source_name = "不应调用"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            raise AssertionError("provider should not be called")

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            raise AssertionError("provider should not be called")

    async def run_check(path: Path) -> None:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"bad": ProviderShouldNotRun()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "bad")],
        )

        with pytest.raises(ValueError, match="limit 必须大于 0"):
            await coordinator.kline("600519.SH", limit=0, use_cache=False)
        with pytest.raises(ValueError, match="limit 必须大于 0"):
            await coordinator.minute_kline("600519.SH", interval="5m", limit=-1, use_cache=False)

    with TemporaryDirectory() as tmpdir:
        asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))


def test_kline_coordinator_propagates_cancellation_without_provider_failure() -> None:
    class CancellingProvider:
        source_name = "取消源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            raise asyncio.CancelledError()

    async def run_check(path: Path) -> list[str]:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = KlineCoordinator(
            settings=settings,
            cache=cache,
            providers={"cancel": CancellingProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "cancel")],
        )

        with pytest.raises(asyncio.CancelledError):
            await coordinator.kline("600519.SH", limit=20, use_cache=False)
        return [item.name for item in cache.provider_capability_statuses()]

    with TemporaryDirectory() as tmpdir:
        recorded_names = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert recorded_names == []


def test_kline_cache_rejects_non_positive_limits() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        cache.save_klines(
            "600519.SH",
            [make_kline(date=f"2026-05-{index + 1:02d}") for index in range(3)],
            "测试日线",
        )
        cache.save_minute_klines(
            "600519.SH",
            "5m",
            [_minute_row(timestamp=f"2026-05-13 10:0{index}:00", interval="5m") for index in range(3)],
            "测试分钟线",
        )

        assert len(cache.get_klines("600519.SH", limit=2, max_age_seconds=10**9)) == 2
        assert len(cache.get_minute_klines("600519.SH", "5m", limit=2, max_age_seconds=10**9)) == 2
        for limit in (0, -1):
            assert cache.get_klines("600519.SH", limit=limit, max_age_seconds=10**9) == []
            assert cache.get_minute_klines("600519.SH", "5m", limit=limit, max_age_seconds=10**9) == []
        for max_age_seconds in (0, -1):
            assert cache.get_klines("600519.SH", limit=2, max_age_seconds=max_age_seconds) == []
            assert cache.get_minute_klines("600519.SH", "5m", limit=2, max_age_seconds=max_age_seconds) == []


def test_kline_cache_filters_invalid_ohlc_and_non_finite_rows() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        valid_daily = make_kline(date="2026-05-13", close=100)
        invalid_daily = make_kline(date="2026-05-14", close=101).model_copy(update={"high": math.inf})
        invalid_volume = make_kline(date="2026-05-15", close=102, volume=-1)
        cache.save_klines("600519.SH", [valid_daily, invalid_daily, invalid_volume], "测试日线")

        valid_minute = _minute_row(timestamp="2026-05-13 10:00:00", interval="5m")
        invalid_minute = _minute_row(timestamp="2026-05-13 10:05:00", interval="5m").model_copy(update={"open": 200.0})
        invalid_amount = _minute_row(timestamp="2026-05-13 10:10:00", interval="5m").model_copy(update={"amount": math.inf})
        cache.save_minute_klines("600519.SH", "5m", [valid_minute, invalid_minute, invalid_amount], "测试分钟线")

        fetched_at = now_text()
        with sqlite3.connect(path) as conn:
            conn.execute(
                """
                    INSERT INTO kline_daily (symbol, date, open, close, high, low, volume, source, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("600519.SH", "2026-05-16", 100.0, 101.0, 100.5, 99.0, 1000.0, "旧坏缓存", fetched_at),
            )
            conn.execute(
                """
                INSERT INTO kline_minute (
                    symbol, interval, timestamp, open, close, high, low, volume, amount, turnover_rate, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("600519.SH", "5m", "2026-05-13 10:15:00", 100.0, 101.0, 102.0, 99.0, math.inf, 101000.0, None, "旧坏缓存", fetched_at),
            )

        daily_rows = cache.get_klines("600519.SH", limit=10, max_age_seconds=10**9)
        minute_rows = cache.get_minute_klines("600519.SH", "5m", limit=10, max_age_seconds=10**9)

    assert [item.date for item in daily_rows] == ["2026-05-13"]
    assert [item.timestamp for item in minute_rows] == ["2026-05-13 10:00:00"]


def test_kline_cache_limits_recent_rows_before_filtering_and_returns_chronological_rows() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        fetched_at = now_text()
        with sqlite3.connect(path) as conn:
            conn.executemany(
                """
                INSERT INTO kline_daily (
                    symbol, adjustment_mode, date, open, close, high, low, volume,
                    as_of, data_version, contract_version, source, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("600519.SH", "qfq", "2026-05-10", 100.0, 101.0, 102.0, 99.0, 1000.0, "2026-05-13", "test-qfq-v1", "daily-kline.v1", "旧缓存", fetched_at),
                    ("600519.SH", "qfq", "2026-05-11", 101.0, 102.0, 103.0, 100.0, 1000.0, "2026-05-13", "test-qfq-v1", "daily-kline.v1", "旧缓存", fetched_at),
                    ("600519.SH", "qfq", "2026-05-12", 102.0, 103.0, 102.5, 101.0, 1000.0, "2026-05-13", "test-qfq-v1", "daily-kline.v1", "坏缓存", fetched_at),
                    ("600519.SH", "qfq", "2026-05-13", 103.0, 104.0, 105.0, 102.0, 1000.0, "2026-05-13", "test-qfq-v1", "daily-kline.v1", "新缓存", fetched_at),
                ],
            )
            conn.executemany(
                """
                INSERT INTO kline_minute (
                    symbol, interval, timestamp, open, close, high, low, volume, amount, turnover_rate, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "600519.SH",
                        "5m",
                        "2026-05-13 10:00:00",
                        100.0,
                        101.0,
                        102.0,
                        99.0,
                        1000.0,
                        101000.0,
                        None,
                        "旧缓存",
                        fetched_at,
                    ),
                    (
                        "600519.SH",
                        "5m",
                        "2026-05-13 10:05:00",
                        101.0,
                        102.0,
                        103.0,
                        100.0,
                        1000.0,
                        102000.0,
                        None,
                        "旧缓存",
                        fetched_at,
                    ),
                    (
                        "600519.SH",
                        "5m",
                        "2026-05-13 10:10:00",
                        102.0,
                        103.0,
                        104.0,
                        101.0,
                        math.inf,
                        103000.0,
                        None,
                        "坏缓存",
                        fetched_at,
                    ),
                    (
                        "600519.SH",
                        "5m",
                        "2026-05-13 10:15:00",
                        103.0,
                        104.0,
                        105.0,
                        102.0,
                        1000.0,
                        104000.0,
                        None,
                        "新缓存",
                        fetched_at,
                    ),
                ],
            )

        daily_rows = cache.get_klines("600519.SH", limit=3, max_age_seconds=10**9)
        minute_rows = cache.get_minute_klines("600519.SH", "5m", limit=3, max_age_seconds=10**9)

    assert [item.date for item in daily_rows] == ["2026-05-11", "2026-05-13"]
    assert [item.timestamp for item in minute_rows] == ["2026-05-13 10:05:00", "2026-05-13 10:15:00"]


def test_kline_cache_rejects_future_fetch_timestamps() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        cache.save_klines("600519.SH", [make_kline(date="2026-05-13")], "测试日线")
        cache.save_minute_klines(
            "600519.SH",
            "5m",
            [_minute_row(timestamp="2026-05-13 10:00:00", interval="5m")],
            "测试分钟线",
        )

        assert len(cache.get_klines("600519.SH", limit=1, max_age_seconds=10**9)) == 1
        assert len(cache.get_minute_klines("600519.SH", "5m", limit=1, max_age_seconds=10**9)) == 1

        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE kline_daily SET fetched_at = ?", (future,))
            conn.execute("UPDATE kline_minute SET fetched_at = ?", (future,))

        assert cache.get_klines("600519.SH", limit=1, max_age_seconds=10**9) == []
        assert cache.get_minute_klines("600519.SH", "5m", limit=1, max_age_seconds=10**9) == []


def test_cache_stats_keeps_daily_and_minute_kline_freshness_separate() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        cache.save_klines("600519.SH", [make_kline(date="2026-05-13")], "测试日线")
        cache.save_minute_klines(
            "600519.SH",
            "5m",
            [_minute_row(timestamp="2026-05-13 10:00:00", interval="5m")],
            "测试分钟线",
        )

        old_daily = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        fresh_minute = now_text()
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE kline_daily SET fetched_at = ?", (old_daily,))
            conn.execute("UPDATE kline_minute SET fetched_at = ?", (fresh_minute,))

        stats = cache.stats()

    assert stats.kline_count == 2
    assert stats.daily_kline_count == 1
    assert stats.minute_kline_count == 1
    assert stats.latest_kline_at == old_daily
    assert stats.latest_daily_kline_at == old_daily
    assert stats.latest_minute_kline_at == fresh_minute


def _minute_row(*, timestamp: str, interval: str) -> MinuteKline:
    return MinuteKline(
        timestamp=timestamp,
        open=100.0,
        close=101.0,
        high=102.0,
        low=99.0,
        volume=1000.0,
        amount=101000.0,
        interval=interval,
    )
