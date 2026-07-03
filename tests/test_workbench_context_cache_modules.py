from __future__ import annotations

import asyncio
import time

from app.services.workbench_context import WorkbenchContextCache
from app.services.datahub import DataHub


def test_datahub_instances_own_separate_workbench_context_caches() -> None:
    first = DataHub()
    second = DataHub()

    assert first.workbench_contexts is not second.workbench_contexts


def test_expired_workbench_context_is_pruned_and_rebuilt() -> None:
    async def run_check():
        cache = WorkbenchContextCache(ttl_seconds=0.01)
        cache.entries["600519.SH"] = (time.monotonic() - 1, "stale")  # type: ignore[assignment]
        build_count = 0

        async def build(symbol: str):
            nonlocal build_count
            build_count += 1
            return f"fresh:{symbol}"  # type: ignore[return-value]

        result = await cache.get("600519", build)
        return result, build_count, cache.entries["600519.SH"][1]

    result, build_count, cached = asyncio.run(run_check())

    assert result == "fresh:600519.SH"
    assert cached == "fresh:600519.SH"
    assert build_count == 1


def test_cancelled_inflight_task_does_not_poison_future_gets() -> None:
    async def run_check():
        cache = WorkbenchContextCache()

        async def cancelled_build():
            await asyncio.sleep(10)
            return "cancelled"  # type: ignore[return-value]

        task = asyncio.create_task(cancelled_build())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        cache._inflight["600519.SH"] = task  # noqa: SLF001

        async def build(symbol: str):
            return f"fresh:{symbol}"  # type: ignore[return-value]

        result = await cache.get("600519", build)
        return result, list(cache._inflight)  # noqa: SLF001

    result, inflight_keys = asyncio.run(run_check())

    assert result == "fresh:600519.SH"
    assert inflight_keys == []


def test_clear_during_inflight_build_prevents_stale_cache_writeback() -> None:
    async def run_check():
        cache = WorkbenchContextCache()
        started = asyncio.Event()
        release = asyncio.Event()

        async def build(symbol: str):
            started.set()
            await release.wait()
            return f"fresh:{symbol}"  # type: ignore[return-value]

        pending = asyncio.create_task(cache.get("600519", build))
        await started.wait()
        cache.clear()
        release.set()
        result = await pending
        return result, dict(cache.entries)

    result, entries = asyncio.run(run_check())

    assert result == "fresh:600519.SH"
    assert entries == {}


def test_concurrent_workbench_context_requests_share_inflight_build() -> None:
    async def run_check():
        cache = WorkbenchContextCache()
        build_count = 0

        async def build(symbol: str):
            nonlocal build_count
            build_count += 1
            await asyncio.sleep(0)
            return f"fresh:{symbol}"  # type: ignore[return-value]

        first, second = await asyncio.gather(cache.get("600519", build), cache.get("600519.SH", build))
        return first, second, build_count

    first, second, build_count = asyncio.run(run_check())

    assert first == "fresh:600519.SH"
    assert second == "fresh:600519.SH"
    assert build_count == 1
