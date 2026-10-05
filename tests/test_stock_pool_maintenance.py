from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models.market import StockInfo
from app.services.cache import SQLiteCache
from app.services.datahub_metadata_coordinator import MetadataCoordinator
from app.services.datahub_metadata_stock_pool import StockPoolResolution
from app.services.datahub_runtime import ProviderRuntime
from app.services.stock_pool_maintenance import refresh_stock_pool_metadata
from app.utils.clock import utc_now
from app.utils.provider_errors import ProviderTransportError


class _Provider:
    source_name = "合成股票池来源"

    def __init__(self, rows: list[StockInfo], error: Exception | None = None) -> None:
        self.rows = rows
        self.error = error
        self.calls = 0

    async def stock_pool(self) -> list[StockInfo]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.rows


def _rows(*, age_days: int = 0, per_market: int = 1) -> list[StockInfo]:
    stamp = (utc_now() - timedelta(days=age_days)).isoformat()
    return [
        StockInfo(
            symbol=f"{base + index:06d}.{market}", code=f"{base + index:06d}", market=market,
            name=f"合成{market}{index}", industry="银行", list_date="2000-01-01",
            source="旧来源" if age_days else _Provider.source_name, updated_at=stamp,
        )
        for market, base in (("SH", 600000), ("SZ", 1), ("BJ", 920001))
        for index in range(per_market)
    ]


def _hub(tmp_path: Path, provider: _Provider, *, cache_type=SQLiteCache) -> SimpleNamespace:
    settings = Settings(
        stock_pool_authoritative_min_count=3, market_scan_min_universe_count=3,
        market_scan_min_sh_count=1, market_scan_min_sz_count=1, market_scan_min_bj_count=1,
        provider_failure_cooldown_seconds=90,
    )
    cache = cache_type(tmp_path / "metadata.sqlite3")
    runtime = ProviderRuntime(cache, settings)
    coordinator = MetadataCoordinator(
        settings=settings, cache=cache, providers={"synthetic": provider}, runtime=runtime,
        priority=lambda kind: [(1, "synthetic")],
    )
    return SimpleNamespace(
        settings=settings, cache=cache, runtime=runtime,
        stock_pool_resolution=coordinator.stock_pool_resolution,
    )


def test_complete_fresh_pool_skips_provider_and_reads_cache_off_event_loop(tmp_path: Path) -> None:
    thread_ids = []

    class TrackedCache(SQLiteCache):
        def get_stock_pool(self, *args, **kwargs):
            thread_ids.append(threading.get_ident())
            return super().get_stock_pool(*args, **kwargs)

    provider = _Provider(_rows())
    hub = _hub(tmp_path, provider, cache_type=TrackedCache)
    hub.cache.save_stock_pool(provider.rows)
    result = asyncio.run(refresh_stock_pool_metadata(hub))
    assert result.status == "success"
    assert "跳过联网刷新" in result.message
    assert provider.calls == 0
    assert thread_ids and threading.get_ident() not in thread_ids


def test_stale_pool_refreshes_all_markets_through_existing_resolver(tmp_path: Path) -> None:
    provider = _Provider(_rows())
    hub = _hub(tmp_path, provider)
    hub.cache.save_stock_pool(_rows(age_days=2))
    original = hub.stock_pool_resolution
    requests = []

    async def resolve(**kwargs):
        requests.append(kwargs)
        return await original(**kwargs)

    hub.stock_pool_resolution = resolve
    result = asyncio.run(refresh_stock_pool_metadata(hub))
    assert result.status == "success" and "已刷新本地股票池" in result.message
    assert provider.calls == 1
    assert requests == [{"limit": None, "refresh": True, "required_markets": frozenset({"SH", "SZ", "BJ"}),
                         "minimum_market_counts": {"SH": 1, "SZ": 1, "BJ": 1}}]
    assert hub.cache.get_stock_pool(86400, limit=None) == sorted(provider.rows, key=lambda row: (row.market, row.code))


def test_fresh_subset_cannot_hide_stale_inventory_rows(tmp_path: Path) -> None:
    provider = _Provider(_rows(per_market=2))
    hub = _hub(tmp_path, provider)
    hub.cache.save_stock_pool(_rows(age_days=2, per_market=2))
    hub.cache.save_stock_pool(_rows())
    assert hub.cache.stock_pool_count(86400) == 3
    result = asyncio.run(refresh_stock_pool_metadata(hub))
    assert result.status == "success" and provider.calls == 1
    assert hub.cache.stock_pool_count(86400) == 6


def test_fresh_pool_missing_market_does_not_skip_full_refresh(tmp_path: Path) -> None:
    provider = _Provider(_rows(per_market=2))
    hub = _hub(tmp_path, provider)
    hub.cache.save_stock_pool([row for row in provider.rows if row.market != "BJ"])
    result = asyncio.run(refresh_stock_pool_metadata(hub))
    assert result.status == "success" and provider.calls == 1
    assert hub.cache.stock_pool_count(86400) == 6


def test_failed_source_preserves_old_dates_and_reuses_capability_cooldown(tmp_path: Path) -> None:
    provider = _Provider([], error=ProviderTransportError("合成网络故障"))
    hub = _hub(tmp_path, provider)
    hub.cache.save_stock_pool(_rows(age_days=2))
    old = hub.cache.get_stock_pool(30 * 86400, limit=None)

    async def check():
        for _ in range(2):
            with pytest.raises(RuntimeError, match="所有股票池数据源均不可用"):
                await refresh_stock_pool_metadata(hub)

    asyncio.run(check())
    assert provider.calls == 1
    assert hub.cache.get_stock_pool(30 * 86400, limit=None) == old
    assert hub.cache.provider_capability_statuses()[0].failure_count == 1


def test_existing_market_shrinkage_guard_rejects_smaller_snapshot(tmp_path: Path) -> None:
    fresh = _rows(per_market=40)
    provider = _Provider([row for row in fresh if not (row.market == "BJ" and row.code >= "920038")])
    hub = _hub(tmp_path, provider)
    hub.cache.save_stock_pool(_rows(age_days=2, per_market=40))
    old = hub.cache.get_stock_pool(30 * 86400, limit=None)
    with pytest.raises(RuntimeError, match="异常缩水"):
        asyncio.run(refresh_stock_pool_metadata(hub))
    assert provider.calls == 1
    assert hub.cache.get_stock_pool(30 * 86400, limit=None) == old


@pytest.mark.parametrize("reason", ["stale-fallback", "provider-partial-pool", "fresh-cache", "keyword-fallback"])
def test_non_full_provider_resolution_is_never_reported_as_refreshed(tmp_path: Path, reason: str) -> None:
    hub = _hub(tmp_path, _Provider(_rows()))

    async def resolve(**kwargs):
        return StockPoolResolution.hit(_rows(), reason)

    hub.stock_pool_resolution = resolve
    result = asyncio.run(refresh_stock_pool_metadata(hub))
    assert result.status == "degraded" and reason in result.message
    assert "已刷新" not in result.message
    assert hub.cache.stock_pool_count() == 0


def test_best_effort_write_failure_does_not_claim_local_pool_was_refreshed(tmp_path: Path) -> None:
    class WriteFailingCache(SQLiteCache):
        def replace_stock_pool(self, rows):
            raise sqlite3.OperationalError("合成本地写入故障")

    provider = _Provider(_rows())
    hub = _hub(tmp_path, provider, cache_type=WriteFailingCache)
    hub.cache.save_stock_pool(_rows(age_days=2))
    old = hub.cache.get_stock_pool(30 * 86400, limit=None)
    result = asyncio.run(refresh_stock_pool_metadata(hub))
    assert result.status == "degraded" and "本地缓存仍未形成" in result.message
    assert provider.calls == 1
    assert hub.cache.get_stock_pool(30 * 86400, limit=None) == old


def test_fresh_pool_metadata_gaps_remain_degraded_without_repeated_requests(tmp_path: Path) -> None:
    provider = _Provider(_rows())
    hub = _hub(tmp_path, provider)
    hub.cache.save_stock_pool([row.model_copy(update={"industry": None}) for row in provider.rows])
    result = asyncio.run(refresh_stock_pool_metadata(hub))
    assert result.status == "degraded" and "行业" in result.message
    assert "跳过联网刷新" in result.message
    assert provider.calls == 0


@pytest.mark.parametrize("stage", ["read", "write"])
@pytest.mark.parametrize("late_failure", [False, True])
def test_cancellation_drains_cache_stage_without_starting_next_phase(tmp_path: Path, stage: str, late_failure: bool) -> None:
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()

    class BlockingCache(SQLiteCache):
        read_calls = 0

        def wait_for_release(self):
            entered.set()
            try:
                assert release.wait(2)
                if late_failure:
                    raise sqlite3.OperationalError("合成取消后缓存错误")
            finally:
                completed.set()

        def get_stock_pool(self, *args, **kwargs):
            self.read_calls += 1
            if stage == "read" and self.read_calls == 1:
                self.wait_for_release()
            return super().get_stock_pool(*args, **kwargs)

        def replace_stock_pool(self, rows):
            self.wait_for_release()
            return super().replace_stock_pool(rows)

    async def scenario():
        provider = _Provider(_rows())
        hub = _hub(tmp_path, provider, cache_type=BlockingCache)
        hub.cache.save_stock_pool(_rows(age_days=2))
        task = asyncio.create_task(refresh_stock_pool_metadata(hub))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            reads = hub.cache.read_calls
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done() and not completed.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert completed.is_set()
            assert hub.cache.read_calls == reads
            assert provider.calls == (1 if stage == "write" else 0)
            assert hub.cache.stock_pool_count(86400) == (3 if stage == "write" and not late_failure else 0)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
