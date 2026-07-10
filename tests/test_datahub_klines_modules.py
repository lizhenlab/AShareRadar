from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import math
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest

from app.config import Settings
from app.models.schemas import Kline, MinuteKline
from app.services.cache import SQLiteCache
from app.services.datahub_klines import DEFAULT_MAX_MINUTE_KLINE_LIMIT, MAX_DAILY_KLINE_LIMIT, KlineCoordinator, _bounded_limit
from app.services.datahub_runtime import ProviderRuntime
from app.services.trading_calendar import latest_expected_trade_date
from app.utils.time import now_text
from tests.factories import make_kline


def test_daily_kline_empty_provider_uses_fallback_cache_and_records_failure() -> None:
    class EmptyKlineProvider:
        source_name = "空K线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return []

    async def run_check(path: Path) -> tuple[list[Kline], bool, int, str | None]:
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
        return rows, runtime.is_cooling("empty", "kline"), status.failure_count, status.last_error

    with TemporaryDirectory() as tmpdir:
        rows, cooling, failure_count, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert len(rows) == 20
    assert all(item.from_cache and item.fallback_used for item in rows)
    assert cooling is True
    assert failure_count == 1
    assert last_error == "K线返回为空"


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
            return [_minute_row(timestamp="2026-05-13 10:00:00", interval=interval)]

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


def test_minute_kline_empty_provider_uses_fallback_cache_and_records_failure() -> None:
    class EmptyMinuteProvider:
        source_name = "空分钟线源"

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            return []

    async def run_check(path: Path) -> tuple[list[MinuteKline], bool, int, str | None]:
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
        return rows, runtime.is_cooling("empty", "minute"), status.failure_count, status.last_error

    with TemporaryDirectory() as tmpdir:
        rows, cooling, failure_count, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert len(rows) == 12
    assert all(item.from_cache and item.fallback_used for item in rows)
    assert cooling is True
    assert failure_count == 1
    assert last_error == "分钟K线返回为空"


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


def test_partial_fresh_cache_does_not_skip_available_provider() -> None:
    class TrackingKlineProvider:
        source_name = "实时K线源"

        def __init__(self, latest_date: str) -> None:
            self.latest_date = datetime.fromisoformat(latest_date).date()
            self.daily_limits: list[int] = []
            self.minute_limits: list[int] = []

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            self.daily_limits.append(limit)
            return [
                make_kline(date=(self.latest_date - timedelta(days=limit - index - 1)).isoformat())
                for index in range(limit)
            ]

        async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
            self.minute_limits.append(limit)
            return [_minute_row(timestamp=f"2026-05-13 10:{index:02d}:00", interval=interval) for index in range(limit)]

    async def run_check(path: Path) -> tuple[int, int, list[int], list[int], str, str]:
        settings = Settings()
        cache = SQLiteCache(path)
        latest = latest_expected_trade_date(datetime.now())
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
        )

        daily = await coordinator.kline("600519.SH", limit=40, use_cache=True)
        minute = await coordinator.minute_kline("600519.SH", interval="5m", limit=20, use_cache=True)
        return len(daily), len(minute), provider.daily_limits, provider.minute_limits, daily[0].source or "", minute[0].source or ""

    with TemporaryDirectory() as tmpdir:
        daily_len, minute_len, daily_limits, minute_limits, daily_source, minute_source = asyncio.run(
            run_check(Path(tmpdir) / "cache.sqlite3")
        )

    assert daily_len == 40
    assert minute_len == 20
    assert daily_limits == [40]
    assert minute_limits == [20]
    assert daily_source == "实时K线源"
    assert minute_source == "实时K线源"


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
        missing_status_exists = any(
            item.name == "missing" and item.kind == "kline" for item in cache.provider_capability_statuses()
        )
        return rows, missing_status_exists

    with TemporaryDirectory() as tmpdir:
        rows, missing_status_exists = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["备用K线源"]
    assert rows[0].from_cache is False
    assert missing_status_exists is False


def test_invalid_provider_kline_rows_are_rejected_before_backup() -> None:
    class InvalidKlineProvider:
        source_name = "坏K线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date="2026-05-13").model_copy(update={"high": math.inf})]

    class BackupKlineProvider:
        source_name = "备用K线源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [make_kline(date="2026-05-14")]

    async def run_check(path: Path) -> tuple[list[Kline], str | None]:
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
        return rows, status.last_error

    with TemporaryDirectory() as tmpdir:
        rows, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["备用K线源"]
    assert last_error == "K线返回为空"


def test_kline_coordinator_filters_sorts_and_limits_provider_rows_before_save() -> None:
    class UnsortedProvider:
        source_name = "乱序源"

        async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
            return [
                make_kline(date="not-a-date"),
                make_kline(date="2026-05-15"),
                make_kline(date="2026-05-13").model_copy(update={"high": math.inf}),
                make_kline(date="2026-05-14"),
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

    assert [item.date for item in daily_rows] == ["2026-05-14", "2026-05-15"]
    assert [item.date for item in cached_daily] == ["2026-05-14", "2026-05-15"]
    assert [item.source for item in daily_rows] == ["乱序源", "乱序源"]
    assert [item.timestamp for item in minute_rows] == ["2026-05-13 10:10:00", "2026-05-13 10:15:00"]
    assert [item.timestamp for item in cached_minute] == ["2026-05-13 10:10:00", "2026-05-13 10:15:00"]
    assert [item.source for item in minute_rows] == ["乱序源", "乱序源"]


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
            return [_minute_row(timestamp="2026-05-13 10:00:00", interval=interval)]

    async def run_check(path: Path) -> tuple[list[int], list[int]]:
        settings = Settings(max_minute_kline_rows=3)
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

    assert daily_limits == [MAX_DAILY_KLINE_LIMIT]
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
                INSERT INTO kline_daily (symbol, date, open, close, high, low, volume, source, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("600519.SH", "2026-05-10", 100.0, 101.0, 102.0, 99.0, 1000.0, "旧缓存", fetched_at),
                    ("600519.SH", "2026-05-11", 101.0, 102.0, 103.0, 100.0, 1000.0, "旧缓存", fetched_at),
                    ("600519.SH", "2026-05-12", 102.0, 103.0, 102.5, 101.0, 1000.0, "坏缓存", fetched_at),
                    ("600519.SH", "2026-05-13", 103.0, 104.0, 105.0, 102.0, 1000.0, "新缓存", fetched_at),
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
