from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import math
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models.schemas import PlateItem, StockConceptItem, StockInfo
from app.services.cache import SQLiteCache
from app.services.datahub_metadata import (
    MetadataCoordinator,
    StockPoolRequest,
    StockPoolResolver,
    _profile_with_local_industry,
    _stock_pool_markets,
)
from app.services.datahub_runtime import ProviderRuntime
from app.services.local_metadata_provider import LocalIndividualStockProvider
from tests.factories import make_plate_item, make_stock_info


def test_plate_rank_empty_provider_uses_backup_and_records_failure() -> None:
    class EmptyPlateProvider:
        source_name = "空板块源"

        async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
            return []

    class BackupPlateProvider:
        source_name = "备用板块源"

        async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
            return [make_plate_item().model_copy(update={"source": self.source_name})]

    async def run_check(path: Path) -> tuple[list[PlateItem], bool, int, str | None]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"empty": EmptyPlateProvider(), "backup": BackupPlateProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "empty"), (2, "backup")],
        )

        rows = await coordinator.plate_rank(limit=5, refresh=True)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "empty" and item.kind == "plate")
        return rows, runtime.is_cooling("empty", "plate"), status.failure_count, status.last_error

    with TemporaryDirectory() as tmpdir:
        rows, cooling, failure_count, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["备用板块源"]
    assert cooling is True
    assert failure_count == 1
    assert last_error == "板块排行返回为空"


def test_plate_rank_returns_provider_rows_when_cache_write_fails() -> None:
    class CacheWriteFailingSQLiteCache(SQLiteCache):
        def save_plate_rank(self, rows: list[PlateItem]) -> None:
            raise sqlite3.DatabaseError("plate cache readonly")

    class LivePlateProvider:
        source_name = "实时板块源"

        async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
            return [make_plate_item().model_copy(update={"source": self.source_name})]

    async def run_check(path: Path) -> tuple[list[PlateItem], bool, int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = CacheWriteFailingSQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"live": LivePlateProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "live")],
        )

        rows = await coordinator.plate_rank(limit=5, refresh=True)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "live" and item.kind == "plate")
        return rows, runtime.is_cooling("live", "plate"), status.failure_count

    with TemporaryDirectory() as tmpdir:
        rows, cooling, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["实时板块源"]
    assert cooling is False
    assert failure_count == 0


def test_plate_rank_result_marks_stale_cache_fallback_without_changing_list_api() -> None:
    class FailingPlateProvider:
        source_name = "失败板块源"

        async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
            raise RuntimeError("plate down")

    async def run_check(path: Path):
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        cache.save_plate_rank([make_plate_item().model_copy(update={"source": "本地缓存", "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})])
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"failed": FailingPlateProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "failed")],
        )
        result = await coordinator.plate_rank_result(limit=5, refresh=True)
        rows = await coordinator.plate_rank(limit=5, refresh=True)
        return result, rows

    with TemporaryDirectory() as tmpdir:
        result, rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert result.used_fallback_cache is True
    assert [(item.source, item.fallback_used) for item in result.rows] == [("本地缓存", True)]
    assert [(item.source, item.fallback_used) for item in rows] == [("本地缓存", True)]


def test_plate_rank_all_invalid_primary_rows_use_backup_without_clearing_cache() -> None:
    class InvalidPlateProvider:
        source_name = "坏板块源"

        async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
            return [
                make_plate_item().model_copy(update={"rank": 0, "name": "无效板块"}),
                make_plate_item().model_copy(update={"rank": 2, "name": " ", "change_pct": math.nan}),
            ]

    class BackupPlateProvider:
        source_name = "备用板块源"

        async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
            return [make_plate_item().model_copy(update={"name": "备用板块", "source": self.source_name})]

    async def run_check(path: Path) -> tuple[list[PlateItem], str | None, list[str]]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        cache.save_plate_rank([make_plate_item().model_copy(update={"name": "旧板块"})])
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"invalid": InvalidPlateProvider(), "backup": BackupPlateProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "invalid"), (2, "backup")],
        )

        rows = await coordinator.plate_rank(limit=5, refresh=True)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "invalid" and item.kind == "plate")
        cached_names = [item.name for item in cache.get_plate_rank(max_age_seconds=10**9, limit=10)]
        return rows, status.last_error, cached_names

    with TemporaryDirectory() as tmpdir:
        rows, last_error, cached_names = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.name for item in rows] == ["备用板块"]
    assert last_error == "板块排行字段无效"
    assert cached_names == ["备用板块"]


def test_stock_pool_returns_provider_rows_when_cache_write_fails() -> None:
    class CacheWriteFailingSQLiteCache(SQLiteCache):
        def save_stock_pool(self, rows: list[StockInfo]) -> None:
            raise sqlite3.DatabaseError("stock pool cache readonly")

    class LiveStockProvider:
        source_name = "实时股票池"

        async def stock_pool(self) -> list[StockInfo]:
            return [make_stock_info(code="600519", market="SH").model_copy(update={"source": self.source_name})]

    async def run_check(path: Path) -> tuple[list[StockInfo], bool, int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = CacheWriteFailingSQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"live": LiveStockProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "live")],
        )

        rows = await coordinator.stock_pool(refresh=True)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "live" and item.kind == "stock")
        return rows, runtime.is_cooling("live", "stock"), status.failure_count

    with TemporaryDirectory() as tmpdir:
        rows, cooling, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["实时股票池"]
    assert cooling is False
    assert failure_count == 0


def test_stock_pool_uses_its_dedicated_long_provider_timeout(tmp_path: Path) -> None:
    class SlowStockProvider:
        source_name = "慢速全量股票池"

        async def stock_pool(self) -> list[StockInfo]:
            await asyncio.sleep(0.03)
            return [make_stock_info(code="600519", market="SH")]

    async def run_check() -> list[StockInfo]:
        settings = Settings(
            provider_call_timeout_seconds=0.01,
            stock_pool_provider_timeout_seconds=1,
        )
        cache = SQLiteCache(tmp_path / "stock-timeout.sqlite3")
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"slow": SlowStockProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "slow")],
        )
        return await coordinator.stock_pool(refresh=True)

    rows = asyncio.run(run_check())

    assert [item.symbol for item in rows] == ["600519.SH"]


def test_stock_pool_unlimited_provider_path_returns_every_row_and_keeps_positive_limit() -> None:
    provider_rows = [
        make_stock_info(code="600519", market="SH"),
        make_stock_info(code="000001", market="SZ"),
        make_stock_info(code="300750", market="SZ"),
    ]

    class LiveStockProvider:
        async def stock_pool(self) -> list[StockInfo]:
            return provider_rows

    async def run_check(path: Path) -> tuple[list[StockInfo], list[StockInfo]]:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"live": LiveStockProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "live")],
        )
        unlimited = await coordinator.stock_pool(limit=None, refresh=True)
        limited = await coordinator.stock_pool(limit=2, refresh=True)
        return unlimited, limited

    with TemporaryDirectory() as tmpdir:
        unlimited, limited = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.symbol for item in unlimited] == ["600519.SH", "000001.SZ", "300750.SZ"]
    assert [item.symbol for item in limited] == ["600519.SH", "000001.SZ"]


def test_stock_pool_market_coverage_rejects_sh_sz_code_exchange_mismatch() -> None:
    mismatched = make_stock_info(code="600519", market="SH").model_copy(update={"symbol": "600519.SZ", "market": "SZ"})

    assert _stock_pool_markets([mismatched]) == set()


def test_stock_pool_required_markets_skips_partial_provider_and_preserves_cached_metadata() -> None:
    calls: list[str] = []

    class PartialProvider:
        async def stock_pool(self) -> list[StockInfo]:
            calls.append("partial")
            return [
                make_stock_info(code="600519", market="SH"),
                make_stock_info(code="000001", market="SZ"),
                make_stock_info(code="920066", market="BJ").model_copy(update={"code": "830001"}),
            ]

    class FullProvider:
        async def stock_pool(self) -> list[StockInfo]:
            calls.append("full")
            return [
                make_stock_info(code="600519", market="SH").model_copy(update={"industry": None, "list_date": None}),
                make_stock_info(code="000001", market="SZ"),
                make_stock_info(code="920066", market="BJ"),
            ]

    async def run_check(path: Path) -> list[StockInfo]:
        settings = Settings()
        cache = SQLiteCache(path)
        cache.save_stock_pool(
            [
                make_stock_info(code="600519", market="SH").model_copy(
                    update={
                        "industry": "白酒",
                        "list_date": "2001-08-27",
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
            ]
        )
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"partial": PartialProvider(), "full": FullProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "partial"), (2, "full")],
        )
        return await coordinator.stock_pool(
            limit=None,
            refresh=True,
            required_markets={"SH", "SZ", "BJ"},
        )

    with TemporaryDirectory() as tmpdir:
        rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == ["partial", "full"]
    assert {item.market for item in rows} == {"SH", "SZ", "BJ"}
    maotai = next(item for item in rows if item.symbol == "600519.SH")
    assert maotai.industry == "白酒"
    assert maotai.list_date == "2001-08-27"


def test_stock_pool_market_minimums_skip_truncated_three_market_provider() -> None:
    calls: list[str] = []
    truncated_rows = [
        make_stock_info(code="600519", market="SH"),
        make_stock_info(code="000001", market="SZ"),
        make_stock_info(code="920066", market="BJ"),
        make_stock_info(code="920066", market="BJ"),
    ]
    full_rows = [
        make_stock_info(code="600519", market="SH"),
        make_stock_info(code="600000", market="SH"),
        make_stock_info(code="000001", market="SZ"),
        make_stock_info(code="300750", market="SZ"),
        make_stock_info(code="920066", market="BJ"),
    ]

    class TruncatedProvider:
        async def stock_pool(self) -> list[StockInfo]:
            calls.append("truncated")
            return truncated_rows

    class FullProvider:
        async def stock_pool(self) -> list[StockInfo]:
            calls.append("full")
            return full_rows

    async def run_check(path: Path) -> tuple[list[StockInfo], list[StockInfo]]:
        settings = Settings(
            stock_pool_authoritative_min_count=5,
            market_scan_min_sh_count=2,
            market_scan_min_sz_count=2,
            market_scan_min_bj_count=1,
        )
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"truncated": TruncatedProvider(), "full": FullProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "truncated"), (2, "full")],
        )
        returned = await coordinator.stock_pool(
            limit=None,
            refresh=True,
            required_markets={"SH", "SZ", "BJ"},
            minimum_market_counts={"SH": 2, "SZ": 2, "BJ": 1},
        )
        return returned, cache.get_stock_pool(max_age_seconds=10**9, limit=None)

    with TemporaryDirectory() as tmpdir:
        returned, cached = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    expected = {item.symbol for item in full_rows}
    assert calls == ["truncated", "full"]
    assert {item.symbol for item in returned} == expected
    assert {item.symbol for item in cached} == expected


def test_stock_pool_skips_provider_that_shrinks_against_authoritative_baseline() -> None:
    fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    baseline = [
        *(make_stock_info(code=f"600{index:03d}", market="SH") for index in range(60)),
        *(make_stock_info(code=f"000{index:03d}", market="SZ") for index in range(1, 51)),
        *(make_stock_info(code=f"920{index:03d}", market="BJ") for index in range(20)),
    ]
    baseline = [item.model_copy(update={"updated_at": fresh_time}) for item in baseline]
    truncated = [
        *(make_stock_info(code=f"600{index:03d}", market="SH") for index in range(45)),
        *(make_stock_info(code=f"000{index:03d}", market="SZ") for index in range(1, 51)),
        *(make_stock_info(code=f"920{index:03d}", market="BJ") for index in range(20)),
    ]
    calls: list[str] = []

    class TruncatedProvider:
        async def stock_pool(self) -> list[StockInfo]:
            calls.append("truncated")
            return truncated

    class CompleteProvider:
        async def stock_pool(self) -> list[StockInfo]:
            calls.append("complete")
            return baseline

    async def run_check(path: Path) -> tuple[list[StockInfo], str | None]:
        settings = Settings(
            stock_pool_authoritative_min_count=100,
            market_scan_min_sh_count=1,
            market_scan_min_sz_count=1,
            market_scan_min_bj_count=1,
        )
        cache = SQLiteCache(path)
        cache.replace_stock_pool(baseline)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={
                "truncated": TruncatedProvider(),
                "complete": CompleteProvider(),
            },
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "truncated"), (2, "complete")],
        )
        rows = await coordinator.stock_pool(
            limit=None,
            refresh=True,
            required_markets={"SH", "SZ", "BJ"},
            minimum_market_counts={"SH": 1, "SZ": 1, "BJ": 1},
        )
        status = next(item for item in cache.provider_capability_statuses() if item.name == "truncated" and item.kind == "stock")
        return rows, status.last_error

    with TemporaryDirectory() as tmpdir:
        rows, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == ["truncated", "complete"]
    assert {item.symbol for item in rows} == {item.symbol for item in baseline}
    assert last_error is not None
    assert "相对最近权威快照异常缩水" in last_error


def test_authoritative_full_stock_pool_snapshot_removes_disappeared_symbols() -> None:
    class FullProvider:
        async def stock_pool(self) -> list[StockInfo]:
            return [
                make_stock_info(code="600519", market="SH"),
                make_stock_info(code="000001", market="SZ"),
                make_stock_info(code="920066", market="BJ"),
            ]

    async def run_check(path: Path) -> tuple[list[StockInfo], list[StockInfo]]:
        settings = Settings(
            stock_pool_authoritative_min_count=3,
            market_scan_min_sh_count=1,
            market_scan_min_sz_count=1,
            market_scan_min_bj_count=1,
        )
        cache = SQLiteCache(path)
        cache.save_stock_pool(
            [
                make_stock_info(code="600519", market="SH"),
                make_stock_info(code="000001", market="SZ"),
                make_stock_info(code="920066", market="BJ"),
                make_stock_info(code="600099", market="SH").model_copy(update={"name": "已从新快照消失"}),
            ]
        )
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"full": FullProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "full")],
        )
        returned = await coordinator.stock_pool(
            limit=None,
            refresh=True,
            required_markets={"SH", "SZ", "BJ"},
        )
        cached = cache.get_stock_pool(max_age_seconds=10**9, limit=None)
        return returned, cached

    with TemporaryDirectory() as tmpdir:
        returned, cached = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    expected = {"600519.SH", "000001.SZ", "920066.BJ"}
    assert {item.symbol for item in returned} == expected
    assert {item.symbol for item in cached} == expected


def test_full_market_stock_pool_does_not_use_month_old_fallback() -> None:
    class FailingProvider:
        async def stock_pool(self) -> list[StockInfo]:
            raise RuntimeError("stock provider down")

    async def run_check(path: Path) -> None:
        settings = Settings(stock_pool_cache_seconds=1)
        cache = SQLiteCache(path)
        stale_time = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        cache.save_stock_pool(
            [
                make_stock_info(code="600519", market="SH").model_copy(update={"updated_at": stale_time}),
                make_stock_info(code="000001", market="SZ").model_copy(update={"updated_at": stale_time}),
                make_stock_info(code="920066", market="BJ").model_copy(update={"updated_at": stale_time}),
            ]
        )
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"failed": FailingProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "failed")],
        )
        with pytest.raises(RuntimeError, match="所有股票池数据源均不可用"):
            await coordinator.stock_pool(
                limit=None,
                refresh=True,
                required_markets={"SH", "SZ", "BJ"},
            )

    with TemporaryDirectory() as tmpdir:
        asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))


def test_stock_pool_unlimited_fresh_cache_returns_every_row_without_provider_call() -> None:
    class ProviderShouldNotRun:
        def __init__(self) -> None:
            self.calls = 0

        async def stock_pool(self) -> list[StockInfo]:
            self.calls += 1
            raise AssertionError("fresh cache should satisfy the unlimited request")

    async def run_check(path: Path) -> tuple[list[StockInfo], int]:
        settings = Settings(stock_pool_cache_seconds=3600)
        cache = SQLiteCache(path)
        fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cache.save_stock_pool(
            [
                make_stock_info(code="600519", market="SH").model_copy(update={"updated_at": fresh_time}),
                make_stock_info(code="600000", market="SH").model_copy(update={"updated_at": fresh_time}),
                make_stock_info(code="000001", market="SZ").model_copy(update={"updated_at": fresh_time}),
            ]
        )
        provider = ProviderShouldNotRun()
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"unused": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "unused")],
        )
        rows = await coordinator.stock_pool(limit=None, refresh=False)
        return rows, provider.calls

    with TemporaryDirectory() as tmpdir:
        rows, provider_calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.symbol for item in rows] == ["600000.SH", "600519.SH", "000001.SZ"]
    assert provider_calls == 0


def test_stock_pool_unlimited_stale_fallback_returns_every_keyword_match() -> None:
    class FailingStockProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def stock_pool(self) -> list[StockInfo]:
            self.calls += 1
            raise RuntimeError("stock provider down")

    async def run_check(path: Path) -> tuple[list[StockInfo], int]:
        settings = Settings(stock_pool_cache_seconds=1)
        cache = SQLiteCache(path)
        stale_time = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        cache.save_stock_pool(
            [
                make_stock_info(code="600000", market="SH").model_copy(update={"name": "浦发银行", "updated_at": stale_time}),
                make_stock_info(code="601398", market="SH").model_copy(update={"name": "工商银行", "updated_at": stale_time}),
                make_stock_info(code="000001", market="SZ").model_copy(update={"name": "平安银行", "updated_at": stale_time}),
                make_stock_info(code="600519", market="SH").model_copy(update={"name": "贵州茅台", "updated_at": stale_time}),
            ]
        )
        provider = FailingStockProvider()
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"failing": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "failing")],
        )
        rows = await coordinator.stock_pool(keyword="银行", limit=None, refresh=True)
        return rows, provider.calls

    with TemporaryDirectory() as tmpdir:
        rows, provider_calls = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.symbol for item in rows] == ["600000.SH", "601398.SH", "000001.SZ"]
    assert provider_calls == 1


def test_metadata_coordinator_offloads_cache_and_runtime_io_from_event_loop_thread() -> None:
    class ThreadTrackingMetadataCache(SQLiteCache):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.io_threads: dict[str, set[int]] = {}

        def _track(self, operation: str) -> None:
            self.io_threads.setdefault(operation, set()).add(threading.get_ident())

        def get_stock_pool(
            self,
            max_age_seconds: int,
            limit: int | None = 5000,
            keyword: str | None = None,
        ) -> list[StockInfo]:
            self._track("get_stock_pool")
            return super().get_stock_pool(max_age_seconds, limit=limit, keyword=keyword)

        def stock_pool_count(self, max_age_seconds: int | None = None) -> int:
            self._track("stock_pool_count")
            return super().stock_pool_count(max_age_seconds)

        def stats(self):
            self._track("stats")
            return super().stats()

        def save_stock_pool(self, rows: list[StockInfo]) -> None:
            self._track("save_stock_pool")
            super().save_stock_pool(rows)

        def get_plate_rank(self, max_age_seconds: int, limit: int = 20) -> list[PlateItem]:
            self._track("get_plate_rank")
            return super().get_plate_rank(max_age_seconds, limit=limit)

        def save_plate_rank(self, rows: list[PlateItem]) -> None:
            self._track("save_plate_rank")
            super().save_plate_rank(rows)

        def get_stock_concepts(
            self,
            symbol: str,
            max_age_seconds: int,
            limit: int = 8,
        ) -> list[StockConceptItem]:
            self._track("get_stock_concepts")
            return super().get_stock_concepts(symbol, max_age_seconds, limit=limit)

        def save_stock_concepts(self, symbol: str, rows: list[StockConceptItem]) -> None:
            self._track("save_stock_concepts")
            super().save_stock_concepts(symbol, rows)

        def update_provider_capability_success(
            self,
            name: str,
            kind: str,
            priority: int,
            latency_ms: float,
        ) -> None:
            self._track("provider_success")
            super().update_provider_capability_success(name, kind, priority, latency_ms)

        def update_provider_capability_failure(
            self,
            name: str,
            kind: str,
            priority: int,
            error: str,
        ) -> None:
            self._track("provider_failure")
            super().update_provider_capability_failure(name, kind, priority, error)

        def log_event(self, category: str, message: str) -> None:
            self._track("log_event")
            super().log_event(category, message)

    class FailingPlateProvider:
        source_name = "失败板块源"

        async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
            raise RuntimeError("plate down")

    class LiveMetadataProvider:
        source_name = "实时元数据源"

        async def stock_pool(self) -> list[StockInfo]:
            return [make_stock_info(code="600519", market="SH").model_copy(update={"source": self.source_name})]

        async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
            return [make_plate_item().model_copy(update={"source": self.source_name})]

        async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
            return [_concept(symbol=symbol, rank=1, name="实时概念", source=self.source_name)]

    async def run_check(path: Path) -> tuple[list[str], dict[str, set[int]], int]:
        settings = Settings(stock_pool_authoritative_min_count=1000)
        cache = ThreadTrackingMetadataCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"akshare": FailingPlateProvider(), "live": LiveMetadataProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "akshare"), (2, "live")] if kind == "plate" else [(1, "live")],
        )
        event_loop_thread = threading.get_ident()

        stocks = await coordinator.stock_pool(keyword="600519", limit=5, refresh=False)
        plates = await coordinator.plate_rank(limit=5, refresh=False)
        concepts = await coordinator.stock_concepts("600519.SH", limit=5, refresh=False)
        values = [stocks[0].symbol, plates[0].source, concepts[0].name]
        return values, cache.io_threads, event_loop_thread

    with TemporaryDirectory() as tmpdir:
        values, io_threads, event_loop_thread = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert values == ["600519.SH", "实时元数据源", "实时概念"]
    assert {
        "get_stock_pool",
        "save_stock_pool",
        "get_plate_rank",
        "save_plate_rank",
        "get_stock_concepts",
        "save_stock_concepts",
        "provider_success",
        "provider_failure",
        "log_event",
    } <= io_threads.keys()
    assert all(event_loop_thread not in thread_ids for thread_ids in io_threads.values())


def test_stock_pool_stale_fallback_ignores_log_event_failure() -> None:
    class LogFailingSQLiteCache(SQLiteCache):
        def log_event(self, category: str, message: str) -> None:
            raise sqlite3.DatabaseError("event log readonly")

    class FailingStockProvider:
        source_name = "失败股票池"

        async def stock_pool(self) -> list[StockInfo]:
            raise RuntimeError("stock pool down")

    async def run_check(path: Path) -> list[StockInfo]:
        settings = Settings()
        cache = LogFailingSQLiteCache(path)
        cache.save_stock_pool(
            [
                make_stock_info(code="600519", market="SH").model_copy(
                    update={"source": "缓存股票池", "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                )
            ]
        )
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"external": FailingStockProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "external")],
        )

        return await coordinator.stock_pool(keyword="600519", limit=5, refresh=True)

    with TemporaryDirectory() as tmpdir:
        rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [(item.symbol, item.source) for item in rows] == [("600519.SH", "缓存股票池")]


def test_stock_profile_local_industry_fallback_ignores_log_event_failure() -> None:
    class LogFailingSQLiteCache(SQLiteCache):
        def log_event(self, category: str, message: str) -> None:
            raise sqlite3.DatabaseError("event log readonly")

    class LiveStockProvider:
        source_name = "实时股票池"

        async def stock_pool(self) -> list[StockInfo]:
            return [make_stock_info(code="600519", market="SH").model_copy(update={"industry": None, "source": self.source_name})]

    class FailingLocalProvider:
        async def stock_pool(self) -> list[StockInfo]:
            raise RuntimeError("local profile down")

    async def run_check(path: Path) -> StockInfo | None:
        settings = Settings()
        cache = LogFailingSQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"external": LiveStockProvider(), "local": FailingLocalProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "external")],
        )

        return await coordinator.stock_profile("600519.SH")

    with TemporaryDirectory() as tmpdir:
        profile = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert profile is not None
    assert profile.symbol == "600519.SH"
    assert profile.industry is None


def test_stock_profile_uses_local_profile_when_primary_pool_has_coverage_miss() -> None:
    class NarrowStockProvider:
        source_name = "窄股票池"

        async def stock_pool(self) -> list[StockInfo]:
            return [
                make_stock_info(code="600519", market="SH").model_copy(update={"source": self.source_name}),
                make_stock_info(code="000001", market="SZ").model_copy(update={"source": self.source_name}),
            ]

    class LocalStockProvider:
        async def stock_pool(self) -> list[StockInfo]:
            return [make_stock_info(code="600706", market="SH").model_copy(update={"name": "曲江文旅", "industry": "旅游酒店", "source": "本地个股资料"})]

    async def run_check(path: Path) -> StockInfo | None:
        settings = Settings(stock_pool_authoritative_min_count=3)
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"external": NarrowStockProvider(), "local": LocalStockProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "external")],
        )

        return await coordinator.stock_profile("600706.SH")

    with TemporaryDirectory() as tmpdir:
        profile = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert profile is not None
    assert profile.symbol == "600706.SH"
    assert profile.name == "曲江文旅"
    assert profile.industry == "旅游酒店"
    assert profile.source == "本地个股资料"


def test_stock_pool_coverage_miss_records_success_and_tries_backup() -> None:
    class NarrowStockProvider:
        source_name = "窄股票池"

        async def stock_pool(self) -> list[StockInfo]:
            return [make_stock_info(code="000001", market="SZ").model_copy(update={"source": self.source_name})]

    class BackupStockProvider:
        source_name = "备用股票池"

        async def stock_pool(self) -> list[StockInfo]:
            return [make_stock_info(code="600519", market="SH").model_copy(update={"source": self.source_name})]

    async def run_check(path: Path) -> tuple[list[StockInfo], bool, int, int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"narrow": NarrowStockProvider(), "backup": BackupStockProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "narrow"), (2, "backup")],
        )

        rows = await coordinator.stock_pool(keyword="600519", limit=5, refresh=True)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "narrow" and item.kind == "stock")
        return rows, runtime.is_cooling("narrow", "stock"), status.success_count, status.failure_count

    with TemporaryDirectory() as tmpdir:
        rows, cooling, success_count, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.symbol for item in rows] == ["600519.SH"]
    assert [item.source for item in rows] == ["备用股票池"]
    assert cooling is False
    assert success_count == 1
    assert failure_count == 0


def test_single_market_pool_above_total_threshold_cannot_confirm_bj_keyword_empty() -> None:
    calls: list[str] = []

    class ShanghaiOnlyProvider:
        async def stock_pool(self) -> list[StockInfo]:
            calls.append("sh-only")
            return [make_stock_info(code=str(600000 + index), market="SH") for index in range(1_000)]

    class BeijingBackupProvider:
        async def stock_pool(self) -> list[StockInfo]:
            calls.append("bj-backup")
            return [make_stock_info(code="920066", market="BJ")]

    async def run_check(path: Path) -> list[StockInfo]:
        settings = Settings(stock_pool_authoritative_min_count=1_000)
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"sh-only": ShanghaiOnlyProvider(), "bj-backup": BeijingBackupProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "sh-only"), (2, "bj-backup")],
        )
        return await coordinator.stock_pool(keyword="920066", limit=10, refresh=True)

    with TemporaryDirectory() as tmpdir:
        rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == ["sh-only", "bj-backup"]
    assert [item.symbol for item in rows] == ["920066.BJ"]


def test_stock_pool_authoritative_provider_miss_returns_empty_without_backup() -> None:
    class CompleteStockProvider:
        source_name = "完整股票池"

        async def stock_pool(self) -> list[StockInfo]:
            return [
                make_stock_info(code="600519", market="SH").model_copy(update={"source": self.source_name}),
                make_stock_info(code="000001", market="SZ").model_copy(update={"source": self.source_name}),
                make_stock_info(code="920066", market="BJ").model_copy(update={"source": self.source_name}),
            ]

    class BackupShouldNotRun:
        def __init__(self) -> None:
            self.calls = 0

        async def stock_pool(self) -> list[StockInfo]:
            self.calls += 1
            raise AssertionError("authoritative miss should not call backup")

    async def run_check(path: Path) -> tuple[list[StockInfo], int, int]:
        settings = Settings(stock_pool_authoritative_min_count=3)
        cache = SQLiteCache(path)
        backup = BackupShouldNotRun()
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"complete": CompleteStockProvider(), "backup": backup},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "complete"), (2, "backup")],
        )

        rows = await coordinator.stock_pool(keyword="688001", limit=10, refresh=True)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "complete" and item.kind == "stock")
        return rows, backup.calls, status.success_count

    with TemporaryDirectory() as tmpdir:
        rows, backup_calls, success_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert rows == []
    assert backup_calls == 0
    assert success_count == 1


def test_stock_pool_selection_state_distinguishes_coverage_miss_from_authoritative_empty() -> None:
    resolver = StockPoolResolver(
        settings=Settings(stock_pool_authoritative_min_count=2),
        cache=SimpleNamespace(),
        providers={},
        runtime=SimpleNamespace(),
        priority=lambda kind: [],
    )
    request = StockPoolRequest(keyword="688001", limit=10, refresh=True)

    coverage_miss = resolver._select_rows([make_stock_info(code="600519", market="SH")], request)
    authoritative_empty = resolver._select_rows(
        [
            make_stock_info(code="600519", market="SH"),
            make_stock_info(code="000001", market="SZ"),
            make_stock_info(code="920066", market="BJ"),
        ],
        request,
    )

    assert coverage_miss.resolved is False
    assert coverage_miss.reason == "provider-coverage-miss"
    assert authoritative_empty.resolved is True
    assert authoritative_empty.reason == "provider-authoritative-empty"
    assert authoritative_empty.list_rows() == []


def test_stock_pool_refresh_still_uses_stale_keyword_fallback_after_provider_failure() -> None:
    class FailingStockProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def stock_pool(self) -> list[StockInfo]:
            self.calls += 1
            raise RuntimeError("stock provider down")

    async def run_check(path: Path) -> tuple[list[StockInfo], int, str | None]:
        settings = Settings(stock_pool_cache_seconds=1)
        cache = SQLiteCache(path)
        stale_time = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        cache.save_stock_pool([make_stock_info(code="600706", market="SH").model_copy(update={"name": "曲江文旅", "updated_at": stale_time})])
        provider = FailingStockProvider()
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"failing": provider},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "failing")],
        )

        rows = await coordinator.stock_pool(keyword="600706", limit=10, refresh=True)
        with sqlite3.connect(path) as conn:
            row = conn.execute("SELECT message FROM cache_event ORDER BY id DESC LIMIT 1").fetchone()
        return rows, provider.calls, row[0] if row else None

    with TemporaryDirectory() as tmpdir:
        rows, calls, message = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.symbol for item in rows] == ["600706.SH"]
    assert calls == 1
    assert message == "股票池数据源失败，使用本地缓存股票池"


def test_stock_concepts_returns_provider_rows_when_cache_write_fails() -> None:
    class CacheWriteFailingSQLiteCache(SQLiteCache):
        def save_stock_concepts(self, symbol: str, rows: list[StockConceptItem]) -> None:
            raise sqlite3.DatabaseError("concept cache readonly")

    class LiveConceptProvider:
        source_name = "实时概念源"

        async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
            return [_concept(symbol=symbol, rank=1, name="实时概念", source=self.source_name)]

    async def run_check(path: Path) -> tuple[list[StockConceptItem], bool, int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = CacheWriteFailingSQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"live": LiveConceptProvider()},
            runtime=runtime,
            priority=lambda kind: [(1, "live")],
        )

        rows = await coordinator.stock_concepts("600519.SH", limit=5, refresh=True)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "live" and item.kind == "concept")
        return rows, runtime.is_cooling("live", "concept"), status.failure_count

    with TemporaryDirectory() as tmpdir:
        rows, cooling, failure_count = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.name for item in rows] == ["实时概念"]
    assert cooling is False
    assert failure_count == 0


def test_stock_concepts_all_invalid_primary_rows_use_backup_without_clearing_cache() -> None:
    class InvalidConceptProvider:
        source_name = "坏概念源"

        async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
            return [
                _concept(symbol=symbol, rank=1, name="坏概念A", source=" "),
                _concept(symbol=symbol, rank=2, name="坏概念B", source=self.source_name).model_copy(update={"change_pct": math.inf, "updated_at": " "}),
            ]

    class BackupConceptProvider:
        source_name = "备用概念源"

        async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
            return [_concept(symbol=symbol, rank=1, name="备用概念", source=self.source_name)]

    async def run_check(path: Path) -> tuple[list[StockConceptItem], str | None, list[str]]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        cache.save_stock_concepts("600519.SH", [_concept(symbol="600519.SH", rank=1, name="旧概念", source="旧源")])
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"invalid": InvalidConceptProvider(), "backup": BackupConceptProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "invalid"), (2, "backup")],
        )

        rows = await coordinator.stock_concepts("600519.SH", limit=5, refresh=True)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "invalid" and item.kind == "concept")
        cached_names = [item.name for item in cache.get_stock_concepts("600519.SH", max_age_seconds=10**9, limit=10)]
        return rows, status.last_error, cached_names

    with TemporaryDirectory() as tmpdir:
        rows, last_error, cached_names = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.name for item in rows] == ["备用概念"]
    assert last_error == "概念归属返回为空"
    assert cached_names == ["备用概念"]


def test_plate_rank_unregistered_priority_provider_is_skipped_before_backup() -> None:
    class BackupPlateProvider:
        source_name = "备用板块源"

        async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
            return [make_plate_item().model_copy(update={"source": self.source_name})]

    async def run_check(path: Path) -> list[PlateItem]:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"backup": BackupPlateProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "missing"), (2, "backup")],
        )

        return await coordinator.plate_rank(limit=5, refresh=True)

    with TemporaryDirectory() as tmpdir:
        rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in rows] == ["备用板块源"]


def test_metadata_missing_capability_is_silent_skip_without_status_noise() -> None:
    class NoMetadataProvider:
        source_name = "无元数据能力"

    class BackupPlateProvider:
        source_name = "备用板块源"

        async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
            return [make_plate_item().model_copy(update={"source": self.source_name})]

    class BackupConceptProvider:
        source_name = "备用概念源"

        async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
            return [_concept(symbol=symbol, rank=1, name="备用概念", source=self.source_name)]

    async def run_check(path: Path) -> tuple[list[PlateItem], list[StockConceptItem], list[str]]:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={
                "no_cap": NoMetadataProvider(),
                "backup_plate": BackupPlateProvider(),
                "backup_concept": BackupConceptProvider(),
            },
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "no_cap"), (2, "backup_plate" if kind == "plate" else "backup_concept")],
        )

        plates = await coordinator.plate_rank(limit=5, refresh=True)
        concepts = await coordinator.stock_concepts("600519.SH", limit=5, refresh=True)
        status_names = [item.name for item in cache.provider_capability_statuses()]
        return plates, concepts, status_names

    with TemporaryDirectory() as tmpdir:
        plates, concepts, status_names = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.source for item in plates] == ["备用板块源"]
    assert [item.source for item in concepts] == ["备用概念源"]
    assert "no_cap" not in status_names


def test_stock_concepts_unregistered_priority_provider_is_skipped_before_backup() -> None:
    class BackupConceptProvider:
        source_name = "备用概念源"

        async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
            return [_concept(symbol=symbol, rank=9, name="白酒", source=self.source_name)]

    async def run_check(path: Path) -> list[StockConceptItem]:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"backup": BackupConceptProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "missing"), (2, "backup")],
        )

        return await coordinator.stock_concepts("600519", limit=5, refresh=True)

    with TemporaryDirectory() as tmpdir:
        rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [(item.symbol, item.rank, item.name, item.source) for item in rows] == [("600519.SH", 1, "白酒", "备用概念源")]


def test_stock_concepts_raise_when_priority_providers_lack_concept_capability() -> None:
    class NoConceptProvider:
        source_name = "无概念能力源"

    async def run_check(path: Path) -> str:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"no_cap": NoConceptProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "no_cap")],
        )

        with pytest.raises(RuntimeError, match=r"概念归属不可用：600519\.SH") as captured:
            await coordinator.stock_concepts("600519.SH", limit=5, refresh=True)
        return str(captured.value)

    with TemporaryDirectory() as tmpdir:
        message = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert "no_cap: 无概念能力源 不支持概念能力" in message


def test_stock_concepts_raise_when_no_concept_provider_is_configured() -> None:
    async def run_check(path: Path) -> str:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [],
        )

        with pytest.raises(RuntimeError, match=r"概念归属不可用：600519\.SH") as captured:
            await coordinator.stock_concepts("600519.SH", limit=5, refresh=True)
        return str(captured.value)

    with TemporaryDirectory() as tmpdir:
        message = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert "概念未配置可用数据源" in message


def test_local_static_metadata_is_not_exposed_as_realtime_plate_or_concept_capability() -> None:
    provider = LocalIndividualStockProvider()

    capability = provider.capability()

    assert capability.stock_pool is True
    assert capability.plate_rank is False
    assert capability.concept_board is False


def test_local_static_concepts_are_rejected_by_metadata_coordinator() -> None:
    async def run_check(path: Path) -> str:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"local": LocalIndividualStockProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "local")],
        )

        with pytest.raises(RuntimeError, match="本地静态资料不提供concept实时涨跌幅") as captured:
            await coordinator.stock_concepts("600706.SH", limit=5, refresh=True)
        return str(captured.value)

    with TemporaryDirectory() as tmpdir:
        message = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert "概念归属不可用" in message


def test_local_stock_concepts_coverage_miss_preserves_stale_cache() -> None:
    class FailingConceptProvider:
        source_name = "失败概念源"

        async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
            raise RuntimeError("concept down")

    async def run_check(path: Path):
        settings = Settings()
        cache = SQLiteCache(path)
        cache.save_stock_concepts(
            "600706.SH",
            [
                _concept(symbol="600706.SH", rank=1, name="历史概念", source="缓存概念").model_copy(
                    update={"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                )
            ],
        )
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"external": FailingConceptProvider(), "local": LocalIndividualStockProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "external"), (2, "local")],
        )

        result = await coordinator.stock_concepts_result("600706.SH", limit=5, refresh=True)
        cached_after = cache.get_stock_concepts("600706.SH", max_age_seconds=10**9, limit=5)
        local_statuses = [item for item in cache.provider_capability_statuses() if item.name == "local" and item.kind == "concept"]
        return result, cached_after, local_statuses

    with TemporaryDirectory() as tmpdir:
        result, cached_after, local_statuses = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert result.used_fallback_cache is True
    assert [(item.name, item.source, item.fallback_used) for item in result.rows] == [("历史概念", "缓存概念", True)]
    assert [(item.name, item.source) for item in cached_after] == [("历史概念", "缓存概念")]
    assert local_statuses == []


def test_stock_concepts_raises_when_sources_fail_without_cache() -> None:
    class FailingConceptProvider:
        source_name = "失败概念源"

        async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
            raise RuntimeError("concept down")

    async def run_check(path: Path) -> tuple[str, int, str | None]:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"external": FailingConceptProvider(), "local": LocalIndividualStockProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "external"), (2, "local")],
        )

        with pytest.raises(RuntimeError, match=r"概念归属不可用：600706\.SH") as captured:
            await coordinator.stock_concepts("600706.SH", limit=5, refresh=True)
        status = next(item for item in cache.provider_capability_statuses() if item.name == "external" and item.kind == "concept")
        return str(captured.value), status.failure_count, status.last_error

    with TemporaryDirectory() as tmpdir:
        message, failure_count, last_error = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert "concept down" in message
    assert failure_count == 1
    assert last_error == "concept down"


def test_stock_pool_unregistered_priority_provider_is_skipped_before_backup() -> None:
    class BackupStockPoolProvider:
        source_name = "备用股票池"

        async def stock_pool(self) -> list[StockInfo]:
            return [make_stock_info(code="688001", market="SH")]

    async def run_check(path: Path) -> list[StockInfo]:
        settings = Settings(stock_pool_authoritative_min_count=10)
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"backup": BackupStockPoolProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "missing"), (2, "backup")],
        )

        return await coordinator.stock_pool(keyword="688001", limit=10, refresh=True)

    with TemporaryDirectory() as tmpdir:
        rows = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert [item.symbol for item in rows] == ["688001.SH"]


def test_profile_with_local_industry_does_not_mutate_primary_profile() -> None:
    primary = make_stock_info().model_copy(update={"industry": None})
    local = make_stock_info().model_copy(update={"industry": "白酒"})

    merged = _profile_with_local_industry(primary, local)

    assert merged is not None
    assert merged.industry == "白酒"
    assert primary.industry is None


def test_profile_with_local_industry_keeps_primary_industry() -> None:
    primary = make_stock_info().model_copy(update={"industry": "主数据行业"})
    local = make_stock_info().model_copy(update={"industry": "本地行业"})

    merged = _profile_with_local_industry(primary, local)

    assert merged is primary
    assert merged.industry == "主数据行业"


def test_profile_with_local_industry_does_not_create_profile_from_local_only() -> None:
    local = make_stock_info().model_copy(update={"industry": "白酒"})

    assert _profile_with_local_industry(None, local) is None


def test_authoritative_stock_profile_miss_is_not_overridden_by_local_provider() -> None:
    class LocalProvider:
        async def stock_pool(self) -> list[StockInfo]:
            return [make_stock_info(code="688001", market="SH")]

    async def run_check(path: Path) -> StockInfo | None:
        settings = Settings(stock_pool_cache_seconds=3600, stock_pool_authoritative_min_count=3)
        cache = SQLiteCache(path)
        fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cache.save_stock_pool(
            [
                make_stock_info(code="600000", market="SH").model_copy(update={"updated_at": fresh_time}),
                make_stock_info(code="000001", market="SZ").model_copy(update={"updated_at": fresh_time}),
                make_stock_info(code="920066", market="BJ").model_copy(update={"updated_at": fresh_time}),
            ]
        )
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"local": LocalProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [],
        )
        return await coordinator.stock_profile("688001.SH")

    with TemporaryDirectory() as tmpdir:
        profile = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert profile is None


def test_authoritative_fresh_stock_pool_empty_result_is_not_overridden_by_stale_match() -> None:
    class ExplodingStockPoolProvider:
        async def stock_pool(self) -> list[StockInfo]:
            raise AssertionError("fresh authoritative cache should answer the miss")

    async def run_check(path: Path) -> tuple[list[StockInfo], StockInfo | None]:
        settings = Settings(stock_pool_cache_seconds=3600, stock_pool_authoritative_min_count=3)
        cache = SQLiteCache(path)
        fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stale_time = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        cache.save_stock_pool(
            [
                make_stock_info(code="600000", market="SH").model_copy(update={"updated_at": fresh_time}),
                make_stock_info(code="000001", market="SZ").model_copy(update={"updated_at": fresh_time}),
                make_stock_info(code="920066", market="BJ").model_copy(update={"updated_at": fresh_time}),
            ]
        )
        cache.save_stock_pool([make_stock_info(code="688001", market="SH").model_copy(update={"updated_at": stale_time})])
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"exploding": ExplodingStockPoolProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "exploding")],
        )
        rows = await coordinator.stock_pool(keyword="688001", limit=10, refresh=False)
        profile = await coordinator.stock_profile("688001.SH")
        return rows, profile

    with TemporaryDirectory() as tmpdir:
        rows, profile = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert rows == []
    assert profile is None


def test_metadata_coordinator_rejects_non_positive_limits_before_provider_calls() -> None:
    class ProviderShouldNotRun:
        source_name = "不应调用"

        async def stock_pool(self) -> list[StockInfo]:
            raise AssertionError("provider should not be called")

        async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
            raise AssertionError("provider should not be called")

        async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
            raise AssertionError("provider should not be called")

    async def run_check(path: Path) -> None:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"bad": ProviderShouldNotRun()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "bad")],
        )

        with pytest.raises(ValueError, match="limit 必须大于 0"):
            await coordinator.stock_pool(limit=0, refresh=True)
        with pytest.raises(ValueError, match="limit 必须大于 0"):
            await coordinator.plate_rank(limit=-1, refresh=True)
        with pytest.raises(ValueError, match="limit 必须大于 0"):
            await coordinator.stock_concepts("600519", limit=0, refresh=True)

    with TemporaryDirectory() as tmpdir:
        asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))


def test_local_metadata_provider_rejects_non_positive_limits() -> None:
    provider = LocalIndividualStockProvider()

    async def run_check() -> None:
        with pytest.raises(ValueError, match="limit 必须大于 0"):
            await provider.plate_rank(limit=0)
        with pytest.raises(ValueError, match="limit 必须大于 0"):
            await provider.stock_concepts("600519", limit=-1)

    asyncio.run(run_check())


def test_local_stock_profile_failure_is_logged() -> None:
    class FailingLocalProvider:
        async def stock_pool(self) -> list[StockInfo]:
            raise RuntimeError("local metadata down")

    async def run_check(path: Path) -> tuple[StockInfo | None, str | None]:
        settings = Settings()
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"local": FailingLocalProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [],
        )

        profile = await coordinator._local_stock_profile("600519.SH")
        with sqlite3.connect(path) as conn:
            row = conn.execute("SELECT message FROM cache_event ORDER BY id DESC LIMIT 1").fetchone()
        return profile, row[0] if row else None

    with TemporaryDirectory() as tmpdir:
        profile, message = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert profile is None
    assert message is not None
    assert "本地个股基础资料不可用" in message
    assert "local metadata down" in message


def test_metadata_cache_rejects_non_positive_limits() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        cache.save_stock_pool([make_stock_info(code="600519", market="SH")])
        cache.save_plate_rank([make_plate_item()])
        cache.save_stock_concepts("600519.SH", [_concept(symbol="600519.SH", rank=1, name="白酒", source="测试概念")])

        assert len(cache.get_stock_pool(max_age_seconds=10**9, limit=1)) == 1
        assert len(cache.get_plate_rank(max_age_seconds=10**9, limit=1)) == 1
        assert len(cache.get_stock_concepts("600519.SH", max_age_seconds=10**9, limit=1)) == 1
        for limit in (0, -1):
            assert cache.get_stock_pool(max_age_seconds=10**9, limit=limit) == []
            assert cache.get_plate_rank(max_age_seconds=10**9, limit=limit) == []
            assert cache.get_stock_concepts("600519.SH", max_age_seconds=10**9, limit=limit) == []
        for max_age_seconds in (0, -1):
            assert cache.get_stock_pool(max_age_seconds=max_age_seconds, limit=1) == []
            assert cache.stock_pool_count(max_age_seconds=max_age_seconds) == 0
            assert cache.get_plate_rank(max_age_seconds=max_age_seconds, limit=1) == []
            assert cache.get_stock_concepts("600519.SH", max_age_seconds=max_age_seconds, limit=1) == []


def test_metadata_cache_and_repository_support_unlimited_stock_pool_keyword_reads() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cache.save_stock_pool(
            [
                make_stock_info(code="600000", market="SH").model_copy(update={"name": "浦发银行", "updated_at": fresh_time}),
                make_stock_info(code="601398", market="SH").model_copy(update={"name": "工商银行", "updated_at": fresh_time}),
                make_stock_info(code="000001", market="SZ").model_copy(update={"name": "平安银行", "updated_at": fresh_time}),
                make_stock_info(code="600519", market="SH").model_copy(update={"name": "贵州茅台", "updated_at": fresh_time}),
            ]
        )

        cached = cache.get_stock_pool(max_age_seconds=3600, limit=None, keyword="银行")
        repository_rows = cache.market_data_repo.get_stock_pool(max_age_seconds=3600, limit=None, keyword="银行")
        limited = cache.get_stock_pool(max_age_seconds=3600, limit=2, keyword="银行")

    expected = ["600000.SH", "601398.SH", "000001.SZ"]
    assert [item.symbol for item in cached] == expected
    assert [item.symbol for item in repository_rows] == expected
    assert [item.symbol for item in limited] == expected[:2]


def test_unlimited_stock_pool_cache_does_not_truncate_after_five_thousand_rows() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        fresh_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cache.save_stock_pool(
            [make_stock_info(code=f"{600000 + index:06d}", market="SH").model_copy(update={"updated_at": fresh_time}) for index in range(5001)]
        )

        unlimited = cache.get_stock_pool(max_age_seconds=3600, limit=None)
        legacy_cap = cache.get_stock_pool(max_age_seconds=3600, limit=5000)

    assert len(unlimited) == 5001
    assert len(legacy_cap) == 5000
    assert unlimited[-1].symbol == "605000.SH"


def test_metadata_cache_rejects_future_update_timestamps() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        cache.save_stock_pool([make_stock_info(code="600519", market="SH")])
        cache.save_plate_rank([make_plate_item()])
        cache.save_stock_concepts("600519.SH", [_concept(symbol="600519.SH", rank=1, name="白酒", source="测试概念")])

        assert len(cache.get_stock_pool(max_age_seconds=10**9, limit=1)) == 1
        assert cache.stock_pool_count(max_age_seconds=10**9) == 1
        assert len(cache.get_plate_rank(max_age_seconds=10**9, limit=1)) == 1
        assert len(cache.get_stock_concepts("600519.SH", max_age_seconds=10**9, limit=1)) == 1

        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE stock_master SET updated_at = ?", (future,))
            conn.execute("UPDATE plate_rank SET updated_at = ?", (future,))
            conn.execute("UPDATE stock_concept SET updated_at = ?", (future,))

        assert cache.get_stock_pool(max_age_seconds=10**9, limit=1) == []
        assert cache.stock_pool_count(max_age_seconds=10**9) == 0
        assert cache.get_plate_rank(max_age_seconds=10**9, limit=1) == []
        assert cache.get_stock_concepts("600519.SH", max_age_seconds=10**9, limit=1) == []


def test_metadata_cache_filters_invalid_rows_and_dedupes_concept_names() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        valid_stock = make_stock_info(code="000002", market="SZ")
        cache.save_stock_pool(
            [
                valid_stock.model_copy(update={"symbol": " ", "code": ""}),
                valid_stock,
            ]
        )
        cache.save_plate_rank(
            [
                make_plate_item().model_copy(update={"rank": 0, "name": "无效板块"}),
                make_plate_item().model_copy(
                    update={
                        "rank": 2,
                        "name": " 有效板块 ",
                        "amount": math.inf,
                        "turnover_rate": -1,
                        "leading_stock_change_pct": math.nan,
                    }
                ),
                make_plate_item().model_copy(update={"rank": 3, "name": " "}),
            ]
        )
        cache.save_stock_concepts(
            "000002.SZ",
            [
                _concept(symbol="000002.SZ", rank=2, name=" 镁金属 ", source="第一源").model_copy(
                    update={
                        "change_pct": 4.0,
                        "amount": -1,
                        "turnover_rate": math.inf,
                        "leading_stock_change_pct": math.nan,
                        "match_reason": "",
                    }
                ),
                _concept(symbol="000002.SZ", rank=1, name="镁金属", source="第二源").model_copy(update={"change_pct": 9.0}),
                _concept(symbol="000002.SZ", rank=1, name=" ", source="空名"),
                _concept(symbol="000002.SZ", rank=1, name="小金属", source="第三源").model_copy(update={"change_pct": 4.0}),
            ],
        )

        assert [item.symbol for item in cache.get_stock_pool(max_age_seconds=10**9, limit=10)] == ["000002.SZ"]
        plates = cache.get_plate_rank(max_age_seconds=10**9, limit=10)
        assert [(item.rank, item.name, item.amount, item.turnover_rate, item.leading_stock_change_pct) for item in plates] == [
            (2, "有效板块", None, None, None)
        ]
        concepts = cache.get_stock_concepts("000002.SZ", max_age_seconds=10**9, limit=10)
        assert [
            (item.name, item.rank, item.source, item.amount, item.turnover_rate, item.leading_stock_change_pct, item.match_reason) for item in concepts
        ] == [
            ("小金属", 1, "第三源", 2_000_000_000, 2.0, 3.0, "测试匹配"),
            ("镁金属", 2, "第一源", None, None, None, "概念成分匹配"),
        ]


def test_metadata_cache_preserves_previous_rows_when_new_rows_are_empty_or_all_invalid() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        original_plate = make_plate_item().model_copy(update={"name": "旧板块", "rank": 1})
        original_concept = _concept(symbol="600519.SH", rank=1, name="旧概念", source="旧源")
        cache.save_plate_rank([original_plate])
        cache.save_stock_concepts("600519.SH", [original_concept])

        cache.save_plate_rank([])
        cache.save_stock_concepts("600519.SH", [])
        assert [item.name for item in cache.get_plate_rank(max_age_seconds=10**9, limit=10)] == ["旧板块"]
        assert [item.name for item in cache.get_stock_concepts("600519.SH", max_age_seconds=10**9, limit=10)] == ["旧概念"]

        cache.save_plate_rank(
            [
                make_plate_item().model_copy(update={"rank": 0, "name": "无效板块"}),
                make_plate_item().model_copy(update={"rank": 2, "name": " ", "change_pct": math.nan}),
            ]
        )
        cache.save_stock_concepts(
            "600519.SH",
            [
                _concept(symbol="600519.SH", rank=0, name="无效概念", source="坏源"),
                _concept(symbol="600519.SH", rank=1, name=" ", source="坏源").model_copy(update={"change_pct": math.inf}),
            ],
        )

        assert [item.name for item in cache.get_plate_rank(max_age_seconds=10**9, limit=10)] == ["旧板块"]
        assert [item.name for item in cache.get_stock_concepts("600519.SH", max_age_seconds=10**9, limit=10)] == ["旧概念"]


def test_metadata_cache_orders_metadata_reads_stably() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        cache.save_stock_pool(
            [
                make_stock_info(code="000002", market="SZ"),
                make_stock_info(code="600519", market="SH"),
                make_stock_info(code="000001", market="SZ"),
            ]
        )
        cache.save_plate_rank(
            [
                make_plate_item().model_copy(update={"rank": 1, "name": "后插入同 rank"}),
                make_plate_item().model_copy(update={"rank": 1, "name": "先后顺序保持"}),
                make_plate_item().model_copy(update={"rank": 2, "name": "第二 rank"}),
            ]
        )
        cache.save_stock_concepts(
            "600519.SH",
            [
                _concept(symbol="600519.SH", rank=1, name="消费B", source="测试概念").model_copy(update={"change_pct": 3.0}),
                _concept(symbol="600519.SH", rank=1, name="消费A", source="测试概念").model_copy(update={"change_pct": 3.0}),
            ],
        )

        assert [item.symbol for item in cache.get_stock_pool(max_age_seconds=10**9, limit=10, keyword="   ")] == [
            "600519.SH",
            "000001.SZ",
            "000002.SZ",
        ]
        assert [item.name for item in cache.get_plate_rank(max_age_seconds=10**9, limit=10)] == [
            "后插入同 rank",
            "先后顺序保持",
            "第二 rank",
        ]
        assert [item.name for item in cache.get_stock_concepts("600519.SH", max_age_seconds=10**9, limit=10)] == [
            "消费A",
            "消费B",
        ]


_FULL_MARKET_MINIMUMS = {"SH": 2, "SZ": 1, "BJ": 1}


def _full_market_consistency_settings() -> Settings:
    return Settings(
        stock_pool_authoritative_min_count=4,
        market_scan_min_sh_count=2,
        market_scan_min_sz_count=1,
        market_scan_min_bj_count=1,
    )


def _full_market_consistency_rows(*, source: str) -> list[StockInfo]:
    return [
        make_stock_info(code="600519", market="SH").model_copy(update={"source": source}),
        make_stock_info(code="600000", market="SH").model_copy(update={"source": source}),
        make_stock_info(code="000001", market="SZ").model_copy(update={"source": source}),
        make_stock_info(code="920066", market="BJ").model_copy(update={"source": source}),
    ]


@pytest.mark.parametrize("missing_field", ["name", "source"])
def test_full_market_coverage_uses_only_rows_with_required_fields(missing_field: str) -> None:
    calls: list[str] = []
    primary_rows = _full_market_consistency_rows(source="primary")
    primary_rows[1] = primary_rows[1].model_copy(update={missing_field: " "})
    backup_rows = _full_market_consistency_rows(source="backup")

    class PrimaryProvider:
        async def stock_pool(self) -> list[StockInfo]:
            calls.append("primary")
            return primary_rows

    class BackupProvider:
        async def stock_pool(self) -> list[StockInfo]:
            calls.append("backup")
            return backup_rows

    async def run_check(path: Path) -> tuple[list[StockInfo], list[StockInfo]]:
        settings = _full_market_consistency_settings()
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"primary": PrimaryProvider(), "backup": BackupProvider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "primary"), (2, "backup")],
        )
        returned = await coordinator.stock_pool(
            limit=None,
            refresh=True,
            required_markets=_FULL_MARKET_MINIMUMS,
            minimum_market_counts=_FULL_MARKET_MINIMUMS,
        )
        persisted = cache.get_stock_pool(max_age_seconds=10**9, limit=None)
        return returned, persisted

    with TemporaryDirectory() as tmpdir:
        returned, persisted = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert calls == ["primary", "backup"]
    assert {item.symbol for item in returned} == {item.symbol for item in persisted}
    assert {item.source for item in returned} == {"backup"}
    assert len(returned) == len(persisted) == 4


def test_full_market_scan_and_persistence_share_one_normalized_deduplicated_pool() -> None:
    rows = _full_market_consistency_rows(source=" primary ")
    rows[0] = rows[0].model_copy(
        update={
            "symbol": " sh600519 ",
            "code": " 600519 ",
            "market": " sh ",
            "name": " first row ",
            "updated_at": " 2026-05-13 10:00:00 ",
        }
    )
    rows.insert(
        1,
        make_stock_info(code="600519", market="SH").model_copy(update={"name": "duplicate row", "source": "duplicate"}),
    )

    class Provider:
        async def stock_pool(self) -> list[StockInfo]:
            return rows

    async def run_check(path: Path) -> tuple[list[StockInfo], list[StockInfo]]:
        settings = _full_market_consistency_settings()
        cache = SQLiteCache(path)
        coordinator = MetadataCoordinator(
            settings=settings,
            cache=cache,
            providers={"provider": Provider()},
            runtime=ProviderRuntime(cache, settings),
            priority=lambda kind: [(1, "provider")],
        )
        returned = await coordinator.stock_pool(
            limit=None,
            refresh=True,
            required_markets=_FULL_MARKET_MINIMUMS,
            minimum_market_counts=_FULL_MARKET_MINIMUMS,
        )
        persisted = cache.get_stock_pool(max_age_seconds=10**9, limit=None)
        return returned, persisted

    with TemporaryDirectory() as tmpdir:
        returned, persisted = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    returned_by_symbol = {item.symbol: item for item in returned}
    persisted_by_symbol = {item.symbol: item for item in persisted}
    assert returned_by_symbol == persisted_by_symbol
    assert len(returned_by_symbol) == 4
    assert returned_by_symbol["600519.SH"].code == "600519"
    assert returned_by_symbol["600519.SH"].market == "SH"
    assert returned_by_symbol["600519.SH"].name == "first row"
    assert returned_by_symbol["600519.SH"].source == "primary"


def test_stock_pool_actual_write_count_mismatch_rolls_back_old_pool() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        old_rows = [make_stock_info(code="600519", market="SH")]
        cache.replace_stock_pool(old_rows)
        with cache._connect() as conn:
            conn.execute(
                """
                CREATE TRIGGER skip_one_stock_master_insert
                BEFORE INSERT ON stock_master
                WHEN NEW.symbol = '000001.SZ'
                BEGIN
                    SELECT RAISE(IGNORE);
                END
                """
            )

        with pytest.raises(RuntimeError, match="写入数量不一致"):
            cache.replace_stock_pool(_full_market_consistency_rows(source="new"))

        persisted = cache.get_stock_pool(max_age_seconds=10**9, limit=None)

    assert [item.symbol for item in persisted] == ["600519.SH"]


def test_stock_pool_insert_failure_rolls_back_old_pool() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        old_rows = [make_stock_info(code="600519", market="SH")]
        cache.replace_stock_pool(old_rows)
        with cache._connect() as conn:
            conn.execute(
                """
                CREATE TRIGGER fail_one_stock_master_insert
                BEFORE INSERT ON stock_master
                WHEN NEW.symbol = '000001.SZ'
                BEGIN
                    SELECT RAISE(ABORT, 'forced stock pool insert failure');
                END
                """
            )

        with pytest.raises(sqlite3.DatabaseError, match="forced stock pool insert failure"):
            cache.replace_stock_pool(_full_market_consistency_rows(source="new"))

        persisted = cache.get_stock_pool(max_age_seconds=10**9, limit=None)

    assert [item.symbol for item in persisted] == ["600519.SH"]


def _concept(*, symbol: str, rank: int, name: str, source: str) -> StockConceptItem:
    return StockConceptItem(
        symbol=symbol,
        rank=rank,
        name=name,
        change_pct=1.0,
        amount=2_000_000_000,
        turnover_rate=2.0,
        leading_stock="测试龙头",
        leading_stock_change_pct=3.0,
        match_reason="测试匹配",
        source=source,
        updated_at="2026-05-13 10:00:00",
    )
