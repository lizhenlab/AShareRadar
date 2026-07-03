from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import math
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest

from app.config import Settings
from app.models.schemas import PlateItem, StockConceptItem, StockInfo
from app.services.cache import SQLiteCache
from app.services.datahub_metadata import MetadataCoordinator, _profile_with_local_industry
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

    assert [(item.symbol, item.rank, item.name, item.source) for item in rows] == [
        ("600519.SH", 1, "白酒", "备用概念源")
    ]


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
                make_stock_info(code=f"60{index:04d}", market="SH").model_copy(update={"updated_at": fresh_time})
                for index in range(3)
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
                _concept(symbol="000002.SZ", rank=1, name="镁金属", source="第二源").model_copy(
                    update={"change_pct": 9.0}
                ),
                _concept(symbol="000002.SZ", rank=1, name=" ", source="空名"),
                _concept(symbol="000002.SZ", rank=1, name="小金属", source="第三源").model_copy(
                    update={"change_pct": 4.0}
                ),
            ],
        )

        assert [item.symbol for item in cache.get_stock_pool(max_age_seconds=10**9, limit=10)] == ["000002.SZ"]
        plates = cache.get_plate_rank(max_age_seconds=10**9, limit=10)
        assert [(item.rank, item.name, item.amount, item.turnover_rate, item.leading_stock_change_pct) for item in plates] == [
            (2, "有效板块", None, None, None)
        ]
        concepts = cache.get_stock_concepts("000002.SZ", max_age_seconds=10**9, limit=10)
        assert [
            (item.name, item.rank, item.source, item.amount, item.turnover_rate, item.leading_stock_change_pct, item.match_reason)
            for item in concepts
        ] == [
            ("小金属", 1, "第三源", 2_000_000_000, 2.0, 3.0, "测试匹配"),
            ("镁金属", 2, "第一源", None, None, None, "概念成分匹配"),
        ]


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
                _concept(symbol="600519.SH", rank=1, name="消费B", source="测试概念").model_copy(
                    update={"change_pct": 3.0}
                ),
                _concept(symbol="600519.SH", rank=1, name="消费A", source="测试概念").model_copy(
                    update={"change_pct": 3.0}
                ),
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
