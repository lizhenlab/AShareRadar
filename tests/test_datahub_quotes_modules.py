from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.config import Settings
from app.models.schemas import Quote
from app.services.cache import SQLiteCache
from app.services.datahub_quotes import QuoteCoordinator
from app.services.datahub_runtime import ProviderRuntime
from tests.factories import make_quote


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
    assert healthy is True
    assert success_count == 1
    assert failure_count == 0
    assert last_error is None
    assert cooling is False


def test_quote_consistency_warning_ignores_monitor_event_write_failure() -> None:
    class DivergentProvider:
        source_name = "差异行情源"

        async def quotes(self, symbols) -> list[Quote]:
            return [_quote_for(symbol, self.source_name).model_copy(update={"price": 1500.0}) for symbol in symbols]

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
