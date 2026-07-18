from __future__ import annotations

import asyncio
import gc
import time

from app.config import Settings
from app.services.cache import SQLiteCache
from app.services.workbench_context import WorkbenchContextCache
from app.services.datahub import DataHub


def test_datahub_instances_own_separate_workbench_context_caches(tmp_path) -> None:
    first_settings = Settings(cache_path=tmp_path / "first.sqlite3", scheduler_enabled=False)
    second_settings = Settings(cache_path=tmp_path / "second.sqlite3", scheduler_enabled=False)
    first = DataHub(cache=SQLiteCache(settings=first_settings), settings=first_settings)
    second = DataHub(cache=SQLiteCache(settings=second_settings), settings=second_settings)

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


def test_cancelled_waiter_does_not_cancel_shared_workbench_build() -> None:
    async def run_check():
        cache = WorkbenchContextCache()
        started = asyncio.Event()
        release = asyncio.Event()
        build_count = 0
        build_cancelled = False

        async def build(symbol: str):
            nonlocal build_count, build_cancelled
            build_count += 1
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                build_cancelled = True
                raise
            return f"fresh:{symbol}"  # type: ignore[return-value]

        cancelled_waiter = asyncio.create_task(cache.get("600519", build))
        surviving_waiter = asyncio.create_task(cache.get("600519.SH", build))
        await started.wait()
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        try:
            await cancelled_waiter
        except asyncio.CancelledError:
            pass
        release.set()
        result = await surviving_waiter
        await asyncio.sleep(0)
        return result, build_count, build_cancelled, dict(cache.entries), dict(cache._inflight)  # noqa: SLF001

    result, build_count, build_cancelled, entries, inflight = asyncio.run(run_check())

    assert result == "fresh:600519.SH"
    assert build_count == 1
    assert build_cancelled is False
    assert entries["600519.SH"][1] == result
    assert inflight == {}


def test_workbench_cache_close_cancels_and_awaits_inflight_builds() -> None:
    async def run_check():
        cache = WorkbenchContextCache()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def build(symbol: str):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return symbol  # pragma: no cover

        waiter = asyncio.create_task(cache.get("600519", build))
        await started.wait()
        await cache.aclose()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        return cancelled.is_set(), dict(cache.entries), dict(cache._inflight)  # noqa: SLF001

    cancelled, entries, inflight = asyncio.run(run_check())

    assert cancelled is True
    assert entries == {}
    assert inflight == {}


def test_workbench_cache_close_is_bounded_and_consumes_late_failure() -> None:
    async def run_check() -> tuple[float, bool, list[dict]]:
        cache = WorkbenchContextCache(shutdown_timeout_seconds=0.01)
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop_errors: list[dict] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def stubborn_build():
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            finally:
                finished.set()
            raise RuntimeError("late workbench close failure")

        task = asyncio.create_task(stubborn_build(), name="stubborn-workbench-build")
        cache._inflight["600519.SH"] = task  # type: ignore[assignment]  # noqa: SLF001
        await started.wait()
        fallback_release = loop.call_later(0.5, release.set)
        began = loop.time()
        await cache.aclose()
        elapsed = loop.time() - began
        release.set()
        fallback_release.cancel()
        await finished.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        del task
        gc.collect()
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)
        assert cache.entries == {}
        assert cache._inflight == {}  # noqa: SLF001
        return elapsed, cancelled.is_set(), loop_errors

    elapsed, cancelled, loop_errors = asyncio.run(run_check())

    assert elapsed < 0.25
    assert cancelled is True
    assert loop_errors == []


def test_cleared_orphaned_build_exception_is_consumed() -> None:
    async def run_check():
        cache = WorkbenchContextCache()
        started = asyncio.Event()
        release = asyncio.Event()
        loop_errors: list[dict] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def build(_symbol: str):
            started.set()
            await release.wait()
            raise RuntimeError("orphaned build failed")

        waiter = asyncio.create_task(cache.get("600519", build))
        await started.wait()
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        cache.clear()
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)
        return loop_errors

    assert asyncio.run(run_check()) == []
