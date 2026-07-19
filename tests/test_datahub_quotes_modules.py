from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time

import pytest

from app.config import Settings
from app.models.schemas import Quote
from app.services import trading_calendar
from app.services.cache import SQLiteCache
from app.services.data_quality_time import (
    normalize_quote_event_time,
    parse_quote_time,
    quote_cache_lookup_seconds,
    quote_event_time_error,
)
from app.services.datahub_quotes import (
    QuoteCoordinator,
    _consistency_freshness_error,
    _quote_now as production_quote_now,
)
from app.services.datahub_runtime import ProviderRuntime
from tests.factories import make_quote


QUOTE_TEST_NOW = datetime(2026, 5, 13, 10, 5)


@pytest.fixture(autouse=True)
def _fixed_quote_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.datahub_quotes._quote_now", lambda: QUOTE_TEST_NOW)


def test_quote_fallback_cache_ignores_log_event_failure() -> None:
    class FailingProvider:
        source_name = "失败行情源"

        async def quotes(self, symbols) -> list[Quote]:
            raise RuntimeError("quote down")

    async def run_check(path: Path) -> list[Quote]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = _LogFailingQuoteCache(path)
        cache.save_quotes([make_quote(source="历史行情")])
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"failing": FailingProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "failing")],
        )

        return await coordinator.quotes(["600519.SH"], use_cache=False)

    with TemporaryDirectory() as tmpdir:
        rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert len(rows) == 1
    assert rows[0].from_cache is True
    assert rows[0].fallback_used is True


def test_quote_partial_success_keeps_provider_healthy_without_cooling_provider() -> None:
    class PartialProvider:
        source_name = "部分行情源"

        async def quotes(self, symbols) -> list[Quote]:
            symbol = symbols[0]
            return [_quote_for(symbol, self.source_name)]

    class BackupProvider:
        source_name = "备用行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbol, self.source_name) for symbol in symbols]

    async def run_check(path: Path) -> tuple[list[Quote], bool, int, int, str | None, bool]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = _LogFailingQuoteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"partial": PartialProvider(), "backup": BackupProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "partial"), (2, "backup")],
        )

        rows = await coordinator.quotes(["600519.SH", "000001.SZ"], use_cache=False)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "partial" and item.kind == "quote")
        return rows, status.healthy, status.success_count, status.failure_count, status.last_error, runtime.is_cooling("partial", "quote")

    with TemporaryDirectory() as tmpdir:
        rows, healthy, success_count, failure_count, last_error, cooling = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["部分行情源", "备用行情源"]
    assert [item.fallback_used for item in rows] == [False, True]
    assert healthy is True
    assert success_count == 1
    assert failure_count == 0
    assert last_error is None
    assert cooling is False


def test_quote_batch_drops_only_bad_event_time_rows_and_falls_back_per_symbol() -> None:
    backup_requests: list[tuple[str, ...]] = []

    class MixedQualityProvider:
        source_name = "混合质量行情源"

        async def quotes(self, symbols) -> list[Quote]:
            timestamps = {
                "600519.SH": "2026-05-13 10:04:00",
                "000001.SZ": "2026-05-12 15:00:00",
                "300750.SZ": "2026-05-13 10:06:00",
                "601318.SH": "not-a-time",
            }
            return [
                _quote_for(symbol, self.source_name).model_copy(
                    update={"timestamp": timestamps[symbol]}
                )
                for symbol in symbols
            ]

    class BackupProvider:
        source_name = "逐股后备行情源"

        async def quotes(self, symbols) -> list[Quote]:
            backup_requests.append(tuple(symbols))
            return [
                _quote_for(symbol, self.source_name).model_copy(
                    update={"timestamp": "2026-05-13 10:04:00"}
                )
                for symbol in symbols
            ]

    async def run_check(path: Path):
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"mixed": MixedQualityProvider(), "backup": BackupProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "mixed"), (2, "backup")],
        )
        symbols = ["600519.SH", "000001.SZ", "300750.SZ", "601318.SH"]
        rows, errors = await coordinator.partial_quotes_with_errors(symbols, use_cache=False)
        status = next(
            item
            for item in cache.provider_capability_statuses()
            if item.name == "mixed" and item.kind == "quote"
        )
        cached = cache.get_quotes(symbols, 10**9)
        with cache._connect() as conn:
            events = [
                str(row[0])
                for row in conn.execute(
                    "SELECT message FROM cache_event WHERE category = 'quote_degraded'"
                )
            ]
        return rows, errors, status, cached, events, runtime.is_cooling("mixed", "quote")

    with TemporaryDirectory() as tmpdir:
        rows, errors, status, cached, events, cooling = asyncio.run(
            run_check(Path(tmpdir) / "cache.sqlite3")
        )

    assert backup_requests == [("000001.SZ", "300750.SZ", "601318.SH")]
    assert [item.source for item in rows] == [
        "混合质量行情源",
        "逐股后备行情源",
        "逐股后备行情源",
        "逐股后备行情源",
    ]
    assert [item.fallback_used for item in rows] == [False, True, True, True]
    assert status.success_count == 1
    assert status.failure_count == 0
    assert status.last_error is None
    assert cooling is False
    assert len(cached) == 4
    assert [item.fallback_used for item in cached] == [False, True, True, True]
    assert any("行质量降级" in item and "000001.SZ" in item for item in errors)
    assert any("300750.SZ" in item and "601318.SH" in item for item in events)


def test_partial_quotes_returns_available_rows_while_strict_quotes_still_rejects_gaps() -> None:
    class PartialOnlyProvider:
        source_name = "部分行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbols[0], self.source_name)]

    async def run_check(path: Path) -> tuple[list[Quote], tuple[str, ...], str]:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"partial": PartialOnlyProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "partial")],
        )
        symbols = ["600519.SH", "000001.SZ"]
        available, errors = await coordinator.partial_quotes_with_errors(symbols, use_cache=False)
        with pytest.raises(RuntimeError) as raised:
            await coordinator.quotes(symbols, use_cache=False)
        return available, errors, str(raised.value)

    with TemporaryDirectory() as tmpdir:
        available, provider_errors, error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [f"{item.code}.{item.market}" for item in available] == ["600519.SH"]
    assert any("000001.SZ" in item for item in provider_errors)
    assert "000001.SZ" in error
    assert "实时行情未完整返回" in error


def test_quote_coordinator_runs_distinct_symbol_requests_concurrently() -> None:
    class ControlledProvider:
        source_name = "可控行情源"

        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []
            self.both_started = asyncio.Event()
            self.release = asyncio.Event()

        async def quotes(self, symbols: list[str]) -> list[Quote]:
            self.calls.append(tuple(symbols))
            if len(self.calls) == 2:
                self.both_started.set()
            await self.release.wait()
            return [_quote_for(symbol, self.source_name) for symbol in symbols]

    async def run_check(path: Path) -> tuple[list[Quote], list[tuple[str, ...]]]:
        settings = Settings(provider_call_timeout_seconds=2)
        cache = SQLiteCache(path)
        provider = ControlledProvider()
        runtime = ProviderRuntime(cache, settings)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"controlled": provider},
            runtime=runtime,
            priority=lambda kind: [(1, "controlled")],
        )
        tasks = [
            asyncio.create_task(coordinator.quote("600706.SH", use_cache=False)),
            asyncio.create_task(coordinator.quote("002182.SZ", use_cache=False)),
        ]
        try:
            await asyncio.wait_for(provider.both_started.wait(), timeout=1)
            provider.release.set()
            rows = await asyncio.gather(*tasks)
            return rows, provider.calls
        finally:
            provider.release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await runtime.aclose()

    with TemporaryDirectory() as tmpdir:
        rows, calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert len(calls) == 2
    assert set(calls) == {("600706.SH",), ("002182.SZ",)}
    assert [(row.code, row.market, row.source) for row in rows] == [
        ("600706", "SH", "可控行情源"),
        ("002182", "SZ", "可控行情源"),
    ]


def test_quote_coordinator_single_flights_same_symbol_request_key() -> None:
    class ControlledProvider:
        source_name = "可控行情源"

        def __init__(self) -> None:
            self.call_count = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def quotes(self, symbols: list[str]) -> list[Quote]:
            self.call_count += 1
            self.started.set()
            await self.release.wait()
            return [_quote_for(symbol, self.source_name) for symbol in symbols]

    class AdmissionTrackingRuntime(ProviderRuntime):
        def __init__(self, cache: SQLiteCache, settings: Settings) -> None:
            super().__init__(cache, settings)
            self.admission_count = 0
            self.both_admitted = asyncio.Event()

        async def _admit_provider_call(self, *args, **kwargs):
            state = await super()._admit_provider_call(*args, **kwargs)
            self.admission_count += 1
            if self.admission_count == 2:
                self.both_admitted.set()
            return state

    async def run_check(path: Path) -> tuple[list[Quote], int]:
        settings = Settings(provider_call_timeout_seconds=2)
        cache = SQLiteCache(path)
        provider = ControlledProvider()
        runtime = AdmissionTrackingRuntime(cache, settings)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"controlled": provider},
            runtime=runtime,
            priority=lambda kind: [(1, "controlled")],
        )
        tasks: list[asyncio.Task[Quote]] = []
        try:
            tasks.append(asyncio.create_task(coordinator.quote("600706.SH", use_cache=False)))
            await asyncio.wait_for(provider.started.wait(), timeout=1)
            tasks.append(asyncio.create_task(coordinator.quote("600706.SH", use_cache=False)))
            await asyncio.wait_for(runtime.both_admitted.wait(), timeout=1)
            assert provider.call_count == 1

            provider.release.set()
            rows = await asyncio.gather(*tasks)
            return rows, provider.call_count
        finally:
            provider.release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await runtime.aclose()

    with TemporaryDirectory() as tmpdir:
        rows, call_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert call_count == 1
    assert [(row.code, row.market, row.source) for row in rows] == [
        ("600706", "SH", "可控行情源"),
        ("600706", "SH", "可控行情源"),
    ]


def test_quote_coordinator_offloads_cache_io_from_event_loop_thread() -> None:
    class ThreadTrackingQuoteCache(SQLiteCache):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.io_threads: dict[str, set[int]] = {}

        def _track(self, operation: str) -> None:
            self.io_threads.setdefault(operation, set()).add(threading.get_ident())

        def get_quotes(self, symbols: list[str], max_age_seconds: int) -> list[Quote]:
            self._track("get_quotes")
            return super().get_quotes(symbols, max_age_seconds)

        def save_quotes(self, quotes: list[Quote]) -> None:
            self._track("save_quotes")
            super().save_quotes(quotes)

        def update_provider_capability_success(
            self,
            name: str,
            kind: str,
            priority: int,
            latency_ms: float,
        ) -> None:
            self._track("provider_success")
            super().update_provider_capability_success(name, kind, priority, latency_ms)

        def log_event(self, category: str, message: str) -> None:
            self._track("log_event")
            super().log_event(category, message)

    class PartialProvider:
        source_name = "部分行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbols[0], self.source_name)]

    class BackupProvider:
        source_name = "备用行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbol, self.source_name) for symbol in symbols]

    async def run_check(path: Path) -> tuple[list[Quote], dict[str, set[int]], int]:
        settings = Settings()
        cache = ThreadTrackingQuoteCache(path)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"partial": PartialProvider(), "backup": BackupProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "partial"), (2, "backup")],
        )
        event_loop_thread = threading.get_ident()

        rows = await coordinator.quotes(["600519.SH", "000001.SZ"], use_cache=True)
        return rows, cache.io_threads, event_loop_thread

    with TemporaryDirectory() as tmpdir:
        rows, io_threads, event_loop_thread = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["部分行情源", "备用行情源"]
    assert {"get_quotes", "save_quotes", "provider_success", "log_event"} <= io_threads.keys()
    assert all(event_loop_thread not in thread_ids for thread_ids in io_threads.values())


def test_quote_coverage_miss_falls_back_without_global_failure_or_cooldown() -> None:
    calls: list[str] = []

    class UncoveredProvider:
        source_name = "不覆盖行情源"

        async def quotes(self, symbols) -> list[Quote]:
            calls.append("uncovered")
            return []

    class BackupProvider:
        source_name = "备用行情源"

        async def quotes(self, symbols) -> list[Quote]:
            calls.append("backup")
            return [_quote_for(symbol, self.source_name) for symbol in symbols]

    async def run_check(path: Path) -> tuple[list[Quote], bool, bool, int, int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"uncovered": UncoveredProvider(), "backup": BackupProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "uncovered"), (2, "backup")],
        )

        rows = await coordinator.quotes(["688001.SH"], use_cache=False)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "uncovered" and item.kind == "quote")
        return rows, runtime.is_cooling("uncovered", "quote"), status.healthy, status.success_count, status.failure_count

    with TemporaryDirectory() as tmpdir:
        rows, cooling, healthy, success_count, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == ["uncovered", "backup"]
    assert rows[0].source == "备用行情源"
    assert rows[0].fallback_used is True
    assert cooling is False
    assert healthy is True
    assert success_count == 1
    assert failure_count == 0


def test_quote_malformed_empty_value_is_protocol_failure_and_cools_provider() -> None:
    class MalformedProvider:
        source_name = "坏结构行情源"

        async def quotes(self, symbols):
            return None

    class BackupProvider:
        source_name = "备用行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbol, self.source_name) for symbol in symbols]

    async def run_check(path: Path) -> tuple[list[Quote], bool, int, str | None]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"malformed": MalformedProvider(), "backup": BackupProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "malformed"), (2, "backup")],
        )

        rows = await coordinator.quotes(["600519.SH"], use_cache=False)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "malformed" and item.kind == "quote")
        return rows, runtime.is_cooling("malformed", "quote"), status.failure_count, status.last_error

    with TemporaryDirectory() as tmpdir:
        rows, cooling, failure_count, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert rows[0].source == "备用行情源"
    assert rows[0].fallback_used is True
    assert cooling is True
    assert failure_count == 1
    assert last_error == "坏结构行情源 行情返回结构异常"


def test_stale_provider_falls_back_before_success_return_and_cache_write() -> None:
    class StaleProvider:
        source_name = "过期行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbol, self.source_name).model_copy(update={"timestamp": "2026-05-12 15:00:00"}) for symbol in symbols]

    class BackupProvider:
        source_name = "新鲜备用源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbol, self.source_name).model_copy(update={"timestamp": "2026-05-13 10:04:00"}) for symbol in symbols]

    async def run_check(path: Path) -> tuple[list[Quote], int, int, list[Quote]]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"stale": StaleProvider(), "backup": BackupProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "stale"), (2, "backup")],
        )

        rows = await coordinator.quotes(["600519.SH"], use_cache=False)
        statuses = {(item.name, item.kind): item for item in cache.provider_capability_statuses()}
        return (
            rows,
            statuses[("stale", "quote")].failure_count,
            statuses[("stale", "quote")].success_count,
            cache.get_quotes(["600519.SH"], 60),
        )

    with TemporaryDirectory() as tmpdir:
        rows, failure_count, success_count, cached = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert rows[0].source == "新鲜备用源"
    assert rows[0].timestamp == "2026-05-13 10:04:00"
    assert failure_count == 1
    assert success_count == 0
    assert [(item.source, item.timestamp) for item in cached] == [("新鲜备用源·缓存", "2026-05-13 10:04:00")]


def test_all_stale_providers_fail_and_do_not_write_quote_cache() -> None:
    class StaleProvider:
        def __init__(self, source_name: str, timestamp: str) -> None:
            self.source_name = source_name
            self.timestamp = timestamp

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbol, self.source_name).model_copy(update={"timestamp": self.timestamp}) for symbol in symbols]

    async def run_check(path: Path) -> tuple[str, int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={
                "stale_one": StaleProvider("过期源一", "2026-05-12 15:00:00"),
                "stale_two": StaleProvider("过期源二", "2026-05-09 15:00:00"),
            },
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "stale_one"), (2, "stale_two")],
        )

        with pytest.raises(RuntimeError) as raised:
            await coordinator.quotes(["600519.SH"], use_cache=False)
        with cache._connect() as conn:
            cache_count = int(conn.execute("SELECT COUNT(*) FROM quote_snapshot").fetchone()[0])
        return str(raised.value), cache_count

    with TemporaryDirectory() as tmpdir:
        error, cache_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert "行情事件时间异常" in error
    assert "早于应参考交易日" in error
    assert cache_count == 0


def test_future_provider_quote_is_protocol_failure_and_falls_back() -> None:
    class FutureProvider:
        source_name = "未来行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbol, self.source_name).model_copy(update={"timestamp": "2026-05-13 10:06:00"}) for symbol in symbols]

    class BackupProvider:
        source_name = "当前行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbol, self.source_name).model_copy(update={"timestamp": "2026-05-13 10:04:00"}) for symbol in symbols]

    async def run_check(path: Path) -> tuple[list[Quote], str | None]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"future": FutureProvider(), "backup": BackupProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "future"), (2, "backup")],
        )
        rows = await coordinator.quotes(["600519.SH"], use_cache=False)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "future" and item.kind == "quote")
        return rows, status.last_error

    with TemporaryDirectory() as tmpdir:
        rows, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert rows[0].source == "当前行情源"
    assert "晚于抓取检查时间" in (last_error or "")


def test_short_cache_rejects_old_quote_timestamp_even_with_fresh_fetched_at() -> None:
    requested: list[str] = []

    class LiveProvider:
        source_name = "实时补齐源"

        async def quotes(self, symbols) -> list[Quote]:
            requested.extend(symbols)
            return [_quote_for(symbol, self.source_name).model_copy(update={"timestamp": "2026-05-13 10:04:00"}) for symbol in symbols]

    async def run_check(path: Path) -> list[Quote]:
        settings = Settings(quote_cache_seconds=60)
        cache = SQLiteCache(path)
        cache.save_quotes([make_quote(source="旧缓存源", timestamp="2026-05-12 15:00:00")])
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"live": LiveProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "live")],
        )
        return await coordinator.quotes(["600519.SH"], use_cache=True)

    with TemporaryDirectory() as tmpdir:
        rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert requested == ["600519.SH"]
    assert rows[0].source == "实时补齐源"
    assert rows[0].from_cache is False


def test_fallback_cache_rejects_dirty_old_event_time_and_uses_calendar_window() -> None:
    class AgeTrackingCache(SQLiteCache):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.max_ages: list[int] = []

        def get_quotes(self, symbols: list[str], max_age_seconds: int) -> list[Quote]:
            self.max_ages.append(max_age_seconds)
            return super().get_quotes(symbols, max_age_seconds)

    class FailingProvider:
        source_name = "失败实时源"

        async def quotes(self, symbols) -> list[Quote]:
            raise RuntimeError("provider down")

    current = datetime(2026, 5, 18, 9, 0)

    async def run_check(path: Path) -> tuple[str, list[int]]:
        settings = Settings()
        cache = AgeTrackingCache(path)
        cache.save_quotes([make_quote(source="旧缓存源", timestamp="2026-05-14 15:00:00")])
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"failing": FailingProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "failing")],
            now=lambda: current,
        )
        with pytest.raises(RuntimeError) as raised:
            await coordinator.quotes(["600519.SH"], use_cache=False)
        return str(raised.value), cache.max_ages

    with TemporaryDirectory() as tmpdir:
        error, max_ages = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert "实时行情未完整返回" in error
    assert max_ages == [quote_cache_lookup_seconds(current)]
    assert max_ages[0] > 24 * 60 * 60


@pytest.mark.parametrize(
    ("current", "event_time"),
    [
        (datetime(2026, 5, 13, 10, 5), "2026-05-13 10:00:00"),
        (datetime(2026, 5, 13, 12, 0), "2026-05-13 11:30:00"),
        (datetime(2026, 5, 13, 16, 0), "2026-05-13 15:00:00"),
        (datetime(2026, 5, 13, 16, 30), "2026-05-13 16:14:00"),
        (datetime(2026, 5, 16, 10, 0), "2026-05-15 15:00:00"),
        (datetime(2026, 5, 16, 10, 0), "2026-05-15 16:14:00"),
        (datetime(2026, 5, 18, 9, 0), "2026-05-15 15:00:00"),
        (datetime(2026, 2, 23, 10, 0), "2026-02-13 15:00:00"),
    ],
)
def test_quote_event_time_accepts_latest_valid_trading_snapshot(
    current: datetime,
    event_time: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trade_days = {
        date(2026, 2, 13),
        date(2026, 2, 24),
        date(2026, 5, 13),
        date(2026, 5, 15),
        date(2026, 5, 18),
    }
    monkeypatch.setattr(trading_calendar, "_trade_days", lambda: trade_days)

    assert quote_event_time_error(event_time, now=current) is None


def test_quote_event_time_normalizes_timezone_aware_values_to_shanghai_naive() -> None:
    utc_event = "2026-05-13T02:04:00+00:00"
    aware_now = datetime(2026, 5, 13, 2, 5, tzinfo=timezone.utc)

    assert normalize_quote_event_time(utc_event) == "2026-05-13 10:04:00"
    assert parse_quote_time(utc_event) == datetime(2026, 5, 13, 10, 4)
    assert quote_event_time_error(utc_event, now=aware_now) is None


def test_quote_production_clock_is_independent_of_host_timezone() -> None:
    original_timezone = os.environ.get("TZ")
    snapshots: list[datetime] = []
    try:
        for timezone_name in ("UTC", "Asia/Shanghai"):
            os.environ["TZ"] = timezone_name
            time.tzset()
            snapshots.append(production_quote_now())
    finally:
        if original_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()

    assert all(item.tzinfo is None for item in snapshots)
    assert abs((snapshots[1] - snapshots[0]).total_seconds()) < 1


def test_quote_consistency_warning_ignores_monitor_event_write_failure() -> None:
    class DivergentProvider:
        source_name = "差异行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbol, self.source_name).model_copy(update={"price": 1500.0, "high": 1501.0}) for symbol in symbols]

    async def run_check(path: Path) -> tuple[str, list[str], int]:
        settings = Settings(quote_consistency_warning_pct=0.01)
        cache = _MonitorFailingQuoteCache(path)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"divergent": DivergentProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "divergent")],
        )

        return await coordinator.consistency(make_quote(source="腾讯行情", price=1000.0))

    with TemporaryDirectory() as tmpdir:
        level, notes, penalty = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert level == "存在差异"
    assert penalty == 18
    assert "超过 0.01% 阈值" in notes[0]


def test_quote_consistency_rejects_secondary_from_different_trade_date() -> None:
    class StaleProvider:
        source_name = "旧交易日行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbol, self.source_name).model_copy(update={"timestamp": "2026-05-12 15:00:00"}) for symbol in symbols]

    async def run_check(path: Path) -> tuple[str, list[str], int, bool, str | None]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"stale": StaleProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "stale")],
        )

        level, notes, penalty = await coordinator.consistency(make_quote(source="腾讯行情", timestamp="2026-05-13 10:00:00"))
        status = next(item for item in cache.provider_capability_statuses() if item.name == "stale" and item.kind == "quote")
        return level, notes, penalty, runtime.is_cooling("stale", "quote"), status.last_error

    with TemporaryDirectory() as tmpdir:
        level, notes, penalty, cooling, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert level == "字段异常"
    assert penalty == 12
    assert "交易日或时间不一致" in notes[0]
    assert cooling is True
    assert "交易日" in (last_error or "")


def test_quote_consistency_coverage_miss_does_not_cool_provider() -> None:
    class UncoveredProvider:
        source_name = "不覆盖行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return []

    async def run_check(path: Path) -> tuple[str, list[str], bool, bool, int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"uncovered": UncoveredProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "uncovered")],
        )

        level, notes, _ = await coordinator.consistency(make_quote(source="腾讯行情"))
        status = next(item for item in cache.provider_capability_statuses() if item.name == "uncovered" and item.kind == "quote")
        return level, notes, runtime.is_cooling("uncovered", "quote"), status.healthy, status.failure_count

    with TemporaryDirectory() as tmpdir:
        level, notes, cooling, healthy, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert level == "单源可用"
    assert "未覆盖" in notes[0]
    assert cooling is False
    assert healthy is True
    assert failure_count == 0


def test_quote_consistency_rejects_excessive_same_day_timestamp_skew() -> None:
    primary = make_quote(timestamp="2026-05-13 10:00:00")
    secondary = make_quote(timestamp="2026-05-13 09:30:00")

    error = _consistency_freshness_error(primary, secondary)

    assert error is not None
    assert "相差约 30 分钟" in error


def test_quote_with_quality_no_cache_does_not_read_cached_klines() -> None:
    class LiveProvider:
        source_name = "实时行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbols[0], self.source_name)]

    async def run_check(path: Path) -> tuple[str, int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = _KlineReadFailingQuoteCache(path)
        coordinator = QuoteCoordinator(
            settings=settings,
            cache=cache,
            providers={"live": LiveProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "live")],
        )

        quote, quality = await coordinator.quote_with_quality("600519.SH", use_cache=False, check_consistency=False)
        return quote.source, quality.kline_count

    with TemporaryDirectory() as tmpdir:
        source, kline_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert source == "实时行情源"
    assert kline_count == 0


def test_single_quote_rejects_blank_symbol_before_batch_lookup() -> None:
    async def run_check(path: Path) -> None:
        cache = SQLiteCache(path)
        coordinator = QuoteCoordinator(
            settings=Settings(),
            cache=cache,
            providers={},
            runtime=ProviderRuntime(cache, Settings()),
            priority=lambda kind: [],
        )
        await coordinator.quote("")

    with TemporaryDirectory() as tmpdir:
        with pytest.raises(ValueError, match="6位数字"):
            asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))


class _LogFailingQuoteCache(SQLiteCache):
    def log_event(self, category: str, message: str) -> None:
        raise RuntimeError("event log down")


class _MonitorFailingQuoteCache(SQLiteCache):
    def save_monitor_event(self, level: str, category: str, message: str, symbol: str | None = None) -> None:
        raise RuntimeError("monitor event down")


class _KlineReadFailingQuoteCache(SQLiteCache):
    def get_klines(self, symbol: str, limit: int, max_age_seconds: int):
        raise AssertionError("cached klines should not be read")


def _quote_for(symbol: str, source: str) -> Quote:
    code, market = symbol.split(".")
    return make_quote(source=source).model_copy(update={"code": code, "market": market})
