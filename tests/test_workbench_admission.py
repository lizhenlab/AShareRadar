from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_datahub
from app.api.routes import stock
from app.services.workbench_context import WorkbenchContextCache
import app.workflows.individual as individual


def test_distinct_symbols_have_bounded_builds_and_busy_calls_never_start() -> None:
    async def check():
        cache = WorkbenchContextCache()
        release = asyncio.Event()
        started: list[str] = []

        async def build(symbol):
            started.append(symbol)
            await release.wait()
            return symbol

        callers = [asyncio.create_task(cache.get(f'{600000+i}.SH', build)) for i in range(12)]
        for _ in range(4):
            await asyncio.sleep(0)
        peak = len(started)
        release.set()
        results = await asyncio.gather(*callers, return_exceptions=True)
        await cache.aclose()
        assert peak == 4
        assert len(started) == 4
        assert sum(isinstance(result, RuntimeError) for result in results) == 8

    asyncio.run(check())


def test_full_budget_still_shares_same_symbol_and_isolates_waiter_cancellation() -> None:
    async def check():
        cache = WorkbenchContextCache(max_inflight=1)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def build(symbol):
            calls.append(symbol)
            started.set()
            await release.wait()
            return symbol

        first = asyncio.create_task(cache.get('600519', build))
        await started.wait()
        peer = asyncio.create_task(cache.get('600519.SH', build, use_cache=False))
        await asyncio.sleep(0)
        try:
            with pytest.raises(RuntimeError, match='并发'):
                await cache.get('000001', build)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        finally:
            release.set()
        assert await peer == '600519.SH'
        assert calls == ['600519.SH']
        await cache.aclose()

    asyncio.run(check())


def test_fresh_cache_read_does_not_spend_build_budget() -> None:
    async def check():
        cache = WorkbenchContextCache(max_inflight=1)
        cache.entries['000001.SZ'] = (time.monotonic(), 'cached')
        started, release = asyncio.Event(), asyncio.Event()

        async def build(symbol):
            started.set()
            await release.wait()
            return symbol

        pending = asyncio.create_task(cache.get('600519', build))
        await started.wait()
        try:
            assert await cache.get('000001', build) == 'cached'
        finally:
            release.set()
            await pending
            await cache.aclose()

    asyncio.run(check())


@pytest.mark.parametrize('error', [RuntimeError, asyncio.CancelledError])
def test_failed_or_cancelled_build_releases_admission(error) -> None:
    async def check():
        cache = WorkbenchContextCache(max_inflight=1)

        async def fails(_symbol):
            raise error()

        async def succeeds(symbol):
            return symbol

        with pytest.raises(error):
            await cache.get('600519', fails)
        assert await cache.get('000001', succeeds) == '000001.SZ'
        await cache.aclose()

    asyncio.run(check())


@pytest.mark.parametrize('use_cache', [True, False])
def test_close_rejects_new_reads_even_if_entries_are_restored(use_cache) -> None:
    async def check():
        cache = WorkbenchContextCache()
        await cache.aclose()
        cache.restore_entries({'600519.SH': (time.monotonic(), 'cached')})
        calls = []

        async def build(symbol):
            calls.append(symbol)
            return symbol

        with pytest.raises(RuntimeError, match='关闭'):
            await cache.get('600519', build, use_cache=use_cache)
        assert calls == []

    asyncio.run(check())


def test_close_wins_against_get_already_waiting_on_admission_lock() -> None:
    async def check():
        cache = WorkbenchContextCache()
        calls = []

        async def build(symbol):
            calls.append(symbol)
            return symbol

        await cache._lock.acquire()
        closing = asyncio.create_task(cache.aclose())
        await asyncio.sleep(0)
        pending = asyncio.create_task(cache.get('600519', build))
        await asyncio.sleep(0)
        cache._lock.release()
        await closing
        with pytest.raises(RuntimeError, match='关闭'):
            await pending
        assert calls == []

    asyncio.run(check())


def test_clear_keeps_running_build_owned_and_counted_until_completion() -> None:
    async def check():
        cache = WorkbenchContextCache(max_inflight=1)
        started, release = asyncio.Event(), asyncio.Event()

        async def build(symbol):
            started.set()
            await release.wait()
            return symbol

        pending = asyncio.create_task(cache.get('600519', build))
        await started.wait()
        cache.clear()
        try:
            with pytest.raises(RuntimeError, match='并发'):
                await cache.get('000001', build)
        finally:
            release.set()
        assert await pending == '600519.SH'
        assert cache.entries == {}
        assert await cache.get('000001', build) == '000001.SZ'
        await cache.aclose()

    asyncio.run(check())


def test_close_still_cancels_build_removed_by_clear() -> None:
    async def check():
        cache = WorkbenchContextCache(shutdown_timeout_seconds=0.01)
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def build(_symbol):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        pending = asyncio.create_task(cache.get('600519', build))
        await started.wait()
        tasks = list(cache._inflight.values())
        cache.clear()
        await cache.aclose()
        was_cancelled = cancelled.is_set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(pending, *tasks, return_exceptions=True)
        assert was_cancelled

    asyncio.run(check())


@pytest.mark.parametrize('limit', [0, -1, True, 1.5, float('inf'), '4'])
def test_build_limit_requires_positive_integer(limit) -> None:
    with pytest.raises(ValueError, match='max_inflight'):
        WorkbenchContextCache(max_inflight=limit)


def test_busy_workbench_api_returns_noncacheable_503_before_new_work(monkeypatch) -> None:
    async def check():
        cache = WorkbenchContextCache(max_inflight=1)
        hub = SimpleNamespace(workbench_contexts=cache)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def build(_hub, symbol):
            calls.append(symbol)
            started.set()
            await release.wait()
            return symbol

        monkeypatch.setattr(individual, '_build_workbench_context', build)
        app = FastAPI()
        app.include_router(stock.router)
        app.dependency_overrides[get_datahub] = lambda: hub
        pending = asyncio.create_task(individual.stock_workbench_context(hub, '600519'))
        await started.wait()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get('/api/stock/workbench?symbol=000001')
            assert response.status_code == 503
            assert response.headers['cache-control'] == 'no-store'
            assert response.json() == {'detail': '个股研究暂不可用，请稍后重试'}
            assert calls == ['600519.SH']
        finally:
            release.set()
            await pending
            await cache.aclose()

    asyncio.run(check())
