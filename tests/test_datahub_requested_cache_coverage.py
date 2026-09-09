from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import sqlite3

from fastapi import FastAPI
import httpx
import pytest

from app.api.deps import get_datahub
from app.api.routes.data import router
from app.config import Settings
from app.models.market import MinuteKline, ProviderCapability
from app.services.cache import SQLiteCache
from app.services.datahub import DataHub
from app.services.datahub_cache_coverage import ShortResponseCoverage, provider_cache_chain
from app.utils.time import now_text
from tests.factories import make_plate_item


NOW = datetime(2026, 9, 8, 10, 20)


class CoverageProvider:
    source_name = "合成覆盖测试源"

    def __init__(self, count=30):
        self.count = count
        self.calls = []
        self.failure = False

    def capability(self):
        return ProviderCapability(name="akshare", installed=True, enabled=True,
                                  minute_kline=True, plate_rank=True, note="synthetic")

    def record(self, limit):
        self.calls.append(limit)
        if self.failure:
            raise RuntimeError("synthetic provider unavailable")

    async def plate_rank(self, limit=20):
        self.record(limit)
        return [make_plate_item().model_copy(update={
            "rank": index + 1, "name": f"合成板块{index}",
            "source": self.source_name, "updated_at": now_text(),
        }) for index in range(min(self.count, limit))]

    async def minute_kline(self, symbol, interval="1m", limit=120):
        self.record(limit)
        return [MinuteKline(
            timestamp=(NOW - timedelta(minutes=index)).isoformat(" "),
            open=10, close=10, high=11, low=9, volume=100, amount=1000,
            source=self.source_name, interval=interval,
        ) for index in reversed(range(min(self.count, limit)))]


@pytest.fixture
def coverage_hub(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config_settings._default_shell_env_values", lambda: {})
    provider = CoverageProvider()
    settings = Settings(cache_path=tmp_path / "coverage.sqlite3",
                        minute_provider_priority=("akshare",), plate_provider_priority=("akshare",),
                        provider_failure_cooldown_seconds=0)
    monkeypatch.setattr("app.services.datahub.build_providers", lambda _settings: {"akshare": provider})
    hub = DataHub(SQLiteCache(settings=settings), settings=settings)
    hub._kline_coordinator._now = lambda: NOW
    return hub, provider


async def _read(hub, kind, limit, *, refresh=False):
    if kind == "minute":
        return await hub.minute_kline("600519.SH", "1m", limit, use_cache=not refresh)
    return await hub.plate_rank(limit, refresh=refresh)


@pytest.mark.parametrize("kind", ["plate", "minute"])
def test_larger_request_fetches_missing_cache_coverage(coverage_hub, kind):
    async def check():
        hub, provider = coverage_hub
        try:
            assert len(await _read(hub, kind, 5)) == 5
            assert len(await _read(hub, kind, 20)) == 20
            assert len(await _read(hub, kind, 10)) == 10
            assert provider.calls == [5, 20]
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["plate", "minute"])
def test_observed_short_provider_response_is_reused_only_up_to_requested_limit(coverage_hub, kind):
    async def check():
        hub, provider = coverage_hub
        provider.count = 3
        try:
            for limit in [20, 20, 10, 20, 25, 25]:
                assert len(await _read(hub, kind, limit)) == 3
            assert provider.calls == [20, 25]
            await _read(hub, kind, 25, refresh=True)
            assert provider.calls == [20, 25, 25]
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["plate", "minute"])
def test_provider_replacement_invalidates_short_response_coverage(coverage_hub, kind):
    async def check():
        hub, provider = coverage_hub
        provider.count = 3
        replacement = CoverageProvider()
        try:
            assert len(await _read(hub, kind, 20)) == 3
            hub.providers["akshare"] = replacement
            assert len(await _read(hub, kind, 20)) == 20
            assert replacement.calls == [20]
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["plate", "minute"])
def test_failed_larger_fetch_returns_explicit_fallback_and_does_not_claim_coverage(coverage_hub, kind):
    async def check():
        hub, provider = coverage_hub
        try:
            assert len(await _read(hub, kind, 5)) == 5
            provider.failure = True
            rows = await _read(hub, kind, 20)
            assert len(rows) == 5
            assert all(row.fallback_used for row in rows)
            provider.failure = False
            assert len(await _read(hub, kind, 20)) == 20
            assert provider.calls == [5, 20, 20]
        finally:
            await hub.aclose()
    asyncio.run(check())


def test_plate_api_larger_limit_expands_cached_result(coverage_hub):
    async def check():
        hub, provider = coverage_hub
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_datahub] = lambda: hub
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
                responses = [await client.get(f"/api/plates?limit={limit}") for limit in [5, 20, 10]]
            assert [response.status_code for response in responses] == [200, 200, 200]
            assert [len(response.json()) for response in responses] == [5, 20, 10]
            assert provider.calls == [5, 20]
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["plate", "minute"])
def test_changed_cached_values_invalidate_observed_short_response(coverage_hub, kind):
    async def check():
        hub, provider = coverage_hub
        provider.count = 3
        try:
            rows = await _read(hub, kind, 20)
            if kind == "plate":
                rows[0] = rows[0].model_copy(update={"change_pct": 2.5})
                hub.cache.save_plate_rank(rows)
            else:
                rows[0] = rows[0].model_copy(update={"close": 10.5})
                hub.cache.save_minute_klines("600519.SH", "1m", rows, provider.source_name)
            assert len(await _read(hub, kind, 20)) == 3
            assert provider.calls == [20, 20]
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["plate", "minute"])
def test_short_response_coverage_does_not_bypass_cache_expiry(coverage_hub, kind):
    async def check():
        hub, provider = coverage_hub
        provider.count = 3
        try:
            assert len(await _read(hub, kind, 20)) == 3
            with sqlite3.connect(hub.cache.path) as conn:
                if kind == "plate":
                    conn.execute("UPDATE plate_rank SET updated_at = '2000-01-01 10:00:00'")
                else:
                    conn.execute("UPDATE kline_minute SET fetched_at = '2000-01-01 10:00:00'")
            assert len(await _read(hub, kind, 20)) == 3
            assert provider.calls == [20, 20]
        finally:
            await hub.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["plate", "minute"])
def test_new_hub_reestablishes_short_response_once_then_reuses_it(coverage_hub, kind):
    async def check():
        hub, provider = coverage_hub
        provider.count = 3
        restarted = None
        try:
            assert len(await _read(hub, kind, 20)) == 3
            restarted = DataHub(hub.cache, settings=hub.settings)
            restarted._kline_coordinator._now = lambda: NOW
            assert len(await _read(restarted, kind, 20)) == 3
            assert len(await _read(restarted, kind, 20)) == 3
            assert provider.calls == [20, 20]
        finally:
            if restarted is not None:
                await restarted.aclose()
            await hub.aclose()
    asyncio.run(check())


def test_short_minute_response_is_reused_across_symbol_and_interval_aliases(coverage_hub):
    async def check():
        hub, provider = coverage_hub
        provider.count = 3
        try:
            assert len(await hub.minute_kline("600519", "1", 20)) == 3
            assert len(await hub.minute_kline("600519.SH", "1m", 20)) == 3
            assert provider.calls == [20]
        finally:
            await hub.aclose()
    asyncio.run(check())


def test_short_response_records_are_bounded_and_bind_provider_order():
    coverage = ShortResponseCoverage(max_entries=2)
    first, second = object(), object()
    providers = {"first": first, "second": second}
    chain = provider_cache_chain([(1, "first"), (2, "second")], providers)
    reversed_chain = provider_cache_chain([(2, "second"), (1, "first")], providers)
    rows = [make_plate_item()]
    coverage.remember("a", rows, 20, chain, ttl_seconds=60)
    coverage.remember("b", rows, 20, chain, ttl_seconds=60)
    assert coverage.covers("a", rows, 20, chain)
    coverage.remember("c", rows, 20, chain, ttl_seconds=60)
    assert not coverage.covers("b", rows, 20, chain)
    assert coverage.covers("a", rows, 20, chain)
    assert not coverage.covers("c", rows, 20, reversed_chain)
    with pytest.raises(ValueError):
        ShortResponseCoverage(max_entries=0)


def test_equivalent_cache_rewrite_cannot_revive_expired_short_response(coverage_hub, monkeypatch):
    tick = [100.0]
    monkeypatch.setattr("app.services.datahub_cache_coverage.monotonic_now", lambda: tick[0], raising=False)

    async def check():
        hub, provider = coverage_hub
        provider.count = 3
        hub.settings.minute_kline_cache_seconds = 60
        try:
            rows = await hub.minute_kline("600519.SH", "1m", 20)
            assert len(rows) == 3
            tick[0] += 61
            # A second cache writer can refresh fetched_at without changing bars.
            other_cache = SQLiteCache(settings=hub.settings)
            other_cache.save_minute_klines("600519.SH", "1m", rows, provider.source_name)
            assert len(other_cache.get_minute_klines("600519.SH", "1m", 20, 60)) == 3
            provider.count = 30
            assert len(await hub.minute_kline("600519.SH", "1m", 20)) == 20
            assert provider.calls == [20, 20]
        finally:
            await hub.aclose()
    asyncio.run(check())
