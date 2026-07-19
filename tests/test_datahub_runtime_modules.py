from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from app.config import Settings
from app.services.datahub import DataHub
from app.services.datahub_runtime import (
    ProviderCallBusyError,
    ProviderCallTimeoutError,
    ProviderRuntime,
    provider_source_name,
    run_provider_io,
)
from app.services.provider_errors import ProviderCoverageMiss


def test_provider_runtime_attempts_are_lazy_and_skip_unavailable_sources() -> None:
    cache = _FailingStatusCache()
    runtime = ProviderRuntime(cache, Settings(provider_failure_cooldown_seconds=30))
    errors: list[str] = []

    attempts = runtime.attempts([(1, "live"), (2, "missing")], {"live": object()}, "quote", errors)
    first = next(attempts)

    assert first.name == "live"
    assert errors == []
    assert list(attempts) == []
    assert errors == ["missing: 数据源未注册"]


def test_provider_runtime_attempts_report_cooling_without_provider_lookup() -> None:
    cache = _FailingStatusCache()
    runtime = ProviderRuntime(cache, Settings(provider_failure_cooldown_seconds=30))
    runtime.record_failure("cooling", 1, RuntimeError("down"), "quote")
    errors: list[str] = []

    attempts = list(runtime.attempts([(1, "cooling"), (2, "live")], {"live": object()}, "quote", errors))

    assert [attempt.name for attempt in attempts] == ["live"]
    assert errors == ["cooling: 最近失败，短暂冷却中"]


def test_provider_runtime_timed_call_returns_value_and_latency() -> None:
    async def run_check() -> tuple[str, float]:
        cache = _FailingStatusCache()
        runtime = ProviderRuntime(cache, Settings(provider_call_timeout_seconds=1))
        result = await runtime.timed_provider_call("test", "quote", lambda: _async_value("ok"))
        return result.value, result.latency_ms

    value, latency_ms = asyncio.run(run_check())

    assert value == "ok"
    assert latency_ms >= 0


def test_provider_runtime_supports_a_longer_timeout_for_full_stock_pool_calls() -> None:
    async def run_check() -> str:
        runtime = ProviderRuntime(
            _FailingStatusCache(),
            Settings(provider_call_timeout_seconds=0.01),
        )

        async def slow_stock_pool() -> str:
            await asyncio.sleep(0.03)
            return "full-pool"

        result = await runtime.timed_provider_call(
            "metadata",
            "stock",
            slow_stock_pool,
            timeout_seconds=0.2,
        )
        return result.value

    assert asyncio.run(run_check()) == "full-pool"


def test_provider_runtime_allows_two_distinct_keys_and_queues_the_third() -> None:
    async def run_check() -> None:
        runtime = ProviderRuntime(_FailingStatusCache(), Settings(provider_call_timeout_seconds=1))
        releases = {key: asyncio.Event() for key in ("first", "second", "third")}
        first_two_started = asyncio.Event()
        third_started = asyncio.Event()
        started: list[str] = []
        callers: list[asyncio.Task[str]] = []

        async def provider_call(key: str) -> str:
            await releases[key].wait()
            return f"{key}-result"

        def start(key: str):
            started.append(key)
            if len(started) == 2:
                first_two_started.set()
            if key == "third":
                third_started.set()
            return provider_call(key)

        try:
            for key in ("first", "second"):
                callers.append(
                    asyncio.create_task(
                        runtime.call_provider(
                            "limited",
                            "quote",
                            lambda key=key: start(key),
                            request_key=key,
                        )
                    )
                )
            await asyncio.wait_for(first_two_started.wait(), timeout=0.2)

            third = asyncio.create_task(
                runtime.call_provider(
                    "limited",
                    "quote",
                    lambda: start("third"),
                    request_key="third",
                )
            )
            callers.append(third)
            await asyncio.sleep(0)

            assert started == ["first", "second"]
            assert third.done() is False

            releases["first"].set()
            assert await callers[0] == "first-result"
            await asyncio.wait_for(third_started.wait(), timeout=0.2)

            assert started == ["first", "second", "third"]
            releases["second"].set()
            releases["third"].set()
            assert await asyncio.gather(*callers[1:]) == ["second-result", "third-result"]
        finally:
            for release in releases.values():
                release.set()
            await asyncio.gather(*callers, return_exceptions=True)
            await runtime.aclose()

    asyncio.run(run_check())


@pytest.mark.parametrize("departing_waiter", ["cancel", "timeout"])
def test_provider_runtime_shared_key_waiter_departure_does_not_cancel_call(
    departing_waiter: str,
) -> None:
    async def run_check() -> None:
        settings = Settings(provider_call_timeout_seconds=0.2)
        runtime = ProviderRuntime(_FailingStatusCache(), settings)
        release = asyncio.Event()
        started = asyncio.Event()
        provider_calls = 0
        first: asyncio.Task[str] | None = None
        second: asyncio.Task[str] | None = None

        async def provider_call() -> str:
            nonlocal provider_calls
            provider_calls += 1
            started.set()
            await release.wait()
            return "shared-result"

        try:
            first = asyncio.create_task(
                runtime.call_provider(
                    "shared",
                    "quote",
                    provider_call,
                    request_key="same-request",
                )
            )
            await asyncio.wait_for(started.wait(), timeout=0.2)

            settings.provider_call_timeout_seconds = 1
            second = asyncio.create_task(
                runtime.call_provider(
                    "shared",
                    "quote",
                    provider_call,
                    request_key="same-request",
                )
            )
            await _wait_for_provider_waiters(runtime, "shared", "quote", "same-request", 2)

            if departing_waiter == "cancel":
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            else:
                with pytest.raises(ProviderCallTimeoutError):
                    await first

            assert provider_calls == 1
            assert second.done() is False

            release.set()
            assert await second == "shared-result"
            assert provider_calls == 1
        finally:
            release.set()
            pending = [task for task in (first, second) if task is not None]
            await asyncio.gather(*pending, return_exceptions=True)
            await runtime.aclose()

    asyncio.run(run_check())


def test_provider_runtime_queue_timeout_busy_does_not_start_cooldown() -> None:
    async def run_check() -> None:
        cache = _FailingStatusCache()
        settings = Settings(
            provider_call_timeout_seconds=0.5,
            provider_failure_cooldown_seconds=30,
        )
        runtime = ProviderRuntime(cache, settings)
        release = asyncio.Event()
        capacity_reached = asyncio.Event()
        started: list[str] = []
        blockers: list[asyncio.Task[str]] = []
        queued_calls = 0

        async def blocking_call(key: str) -> str:
            await release.wait()
            return key

        def start_blocking_call(key: str):
            started.append(key)
            if len(started) == 2:
                capacity_reached.set()
            return blocking_call(key)

        def start_queued_call():
            nonlocal queued_calls
            queued_calls += 1
            return _async_value("queued")

        try:
            for key in ("first", "second"):
                blockers.append(
                    asyncio.create_task(
                        runtime.call_provider(
                            "limited",
                            "quote",
                            lambda key=key: start_blocking_call(key),
                            request_key=key,
                        )
                    )
                )
            await asyncio.wait_for(capacity_reached.wait(), timeout=0.2)

            settings.provider_call_timeout_seconds = 0.02
            with pytest.raises(ProviderCallBusyError, match="当前并发请求较多") as exc_info:
                await runtime.call_provider(
                    "limited",
                    "quote",
                    start_queued_call,
                    request_key="queued",
                )

            errors: list[str] = []
            attempt = next(runtime.attempts([(1, "limited")], {"limited": object()}, "quote", errors))
            await runtime.record_attempt_failure_async(attempt, "quote", exc_info.value, errors)

            assert queued_calls == 0
            assert errors == [f"limited: {exc_info.value}"]
            assert cache.failure_calls == []
            assert runtime.is_cooling("limited", "quote") is False
        finally:
            release.set()
            await asyncio.gather(*blockers, return_exceptions=True)
            await runtime.aclose()

    asyncio.run(run_check())


def test_provider_runtime_orphan_blocks_new_key_but_same_key_can_rejoin() -> None:
    async def run_check() -> None:
        settings = Settings(provider_call_timeout_seconds=0.02)
        runtime = ProviderRuntime(_FailingStatusCache(), settings)
        release = asyncio.Event()
        provider_started = asyncio.Event()
        provider_calls = 0
        different_key_calls = 0
        rejoined: asyncio.Task[str] | None = None

        async def provider_call() -> str:
            nonlocal provider_calls
            provider_calls += 1
            provider_started.set()
            await release.wait()
            return "late-result"

        def start_different_key():
            nonlocal different_key_calls
            different_key_calls += 1
            return _async_value("different-result")

        try:
            with pytest.raises(ProviderCallTimeoutError):
                await runtime.call_provider(
                    "orphaned",
                    "quote",
                    provider_call,
                    request_key="original",
                )
            assert provider_started.is_set()
            assert runtime.provider_call_in_flight("orphaned", "quote") is True

            settings.provider_call_timeout_seconds = 1
            with pytest.raises(ProviderCallBusyError, match="仍在后台执行"):
                await asyncio.wait_for(
                    runtime.call_provider(
                        "orphaned",
                        "quote",
                        start_different_key,
                        request_key="different",
                    ),
                    timeout=0.05,
                )
            assert different_key_calls == 0

            rejoined = asyncio.create_task(
                runtime.call_provider(
                    "orphaned",
                    "quote",
                    provider_call,
                    request_key="original",
                )
            )
            await _wait_for_provider_waiters(runtime, "orphaned", "quote", "original", 1)

            assert provider_calls == 1
            release.set()
            assert await rejoined == "late-result"
            assert provider_calls == 1
        finally:
            release.set()
            if rejoined is not None:
                await asyncio.gather(rejoined, return_exceptions=True)
            await runtime.aclose()

    asyncio.run(run_check())


def test_provider_runtime_timeout_keeps_one_background_sdk_call_per_capability() -> None:
    release = threading.Event()
    worker_started = threading.Event()
    factory_calls = 0
    worker_calls = 0
    counter_lock = threading.Lock()

    def blocking_sdk_call() -> str:
        nonlocal worker_calls
        with counter_lock:
            worker_calls += 1
        worker_started.set()
        release.wait(timeout=2)
        return "late-result"

    def start_quote_call():
        nonlocal factory_calls
        factory_calls += 1

        async def invoke() -> str:
            return await run_provider_io(blocking_sdk_call)

        return invoke()

    async def run_check() -> tuple[int, int]:
        runtime = ProviderRuntime(_FailingStatusCache(), Settings(provider_call_timeout_seconds=0.02))
        try:
            with pytest.raises(ProviderCallTimeoutError, match="后台任务仍在收尾"):
                await runtime.timed_provider_call("slow", "quote", start_quote_call)

            assert worker_started.is_set()
            assert runtime.provider_call_in_flight("slow", "quote") is True

            errors: list[str] = []
            attempts = list(runtime.attempts([(1, "slow")], {"slow": object()}, "quote", errors))
            assert attempts == []
            assert errors == ["slow: 上一次调用仍在后台执行"]

            for _ in range(20):
                with pytest.raises(ProviderCallBusyError, match="仍在后台执行"):
                    await runtime.timed_provider_call("slow", "quote", start_quote_call)

            independent = await runtime.timed_provider_call(
                "slow",
                "kline",
                lambda: _async_value("independent"),
            )
            assert independent.value == "independent"
            assert factory_calls == 1
        finally:
            release.set()
            for _ in range(100):
                if not runtime.provider_call_in_flight("slow", "quote"):
                    break
                await asyncio.sleep(0.005)
            await runtime.aclose()

        assert runtime.provider_call_in_flight("slow", "quote") is False
        return factory_calls, worker_calls

    observed_factory_calls, observed_worker_calls = asyncio.run(run_check())

    assert observed_factory_calls == 1
    assert observed_worker_calls == 1


def test_provider_runtime_uses_owned_bounded_executor_and_closes_idempotently() -> None:
    async def run_check() -> tuple[str, bool]:
        runtime = ProviderRuntime(_FailingStatusCache(), Settings(provider_call_timeout_seconds=1))
        thread_name = await runtime.call_provider(
            "blocking",
            "quote",
            lambda: run_provider_io(lambda: threading.current_thread().name),
        )
        await runtime.aclose()
        await runtime.aclose()
        with pytest.raises(RuntimeError, match="已关闭"):
            await runtime.call_provider("blocking", "quote", lambda: _async_value("late"))
        return thread_name, runtime.provider_call_in_flight("blocking", "quote")

    thread_name, in_flight = asyncio.run(run_check())

    assert thread_name.startswith("ashare-provider")
    assert in_flight is False


def test_stuck_provider_worker_does_not_block_process_exit() -> None:
    script = """
import asyncio
import threading

from app.config import Settings
from app.services.datahub_runtime import ProviderCallTimeoutError, ProviderRuntime, run_provider_io


async def main():
    runtime = ProviderRuntime(object(), Settings(provider_call_timeout_seconds=0.01))
    blocker = threading.Event()
    try:
        await runtime.call_provider(
            "blocking",
            "quote",
            lambda: run_provider_io(blocker.wait),
        )
    except ProviderCallTimeoutError:
        pass
    await runtime.aclose(timeout=0.01)


asyncio.run(main())
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_datahub_close_timeout_auto_closes_provider_before_asyncio_run_finishes(tmp_path) -> None:
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    close_states: list[bool] = []
    provider_closed = asyncio.Event()

    def blocking_call() -> str:
        started.set()
        try:
            release.wait(timeout=2)
        finally:
            finished.set()
        return "done"

    class CloseTrackingProvider:
        async def aclose(self) -> None:
            close_states.append(finished.is_set())
            provider_closed.set()

    async def run_check() -> None:
        settings = Settings(
            cache_path=tmp_path / "cache.sqlite3",
            provider_call_timeout_seconds=0.01,
        )
        hub = DataHub(settings=settings)
        hub.providers = {"slow": CloseTrackingProvider()}
        try:
            with pytest.raises(ProviderCallTimeoutError):
                await hub._provider_runtime.call_provider(
                    "slow",
                    "stock",
                    lambda: run_provider_io(blocking_call),
                )
            assert started.is_set()

            assert await hub.aclose(timeout=0.02) is False
            assert hub._provider_runtime.provider_call_in_flight("slow", "stock") is True
            assert close_states == []
            deferred = hub._provider_close_task
            assert deferred is not None

            release.set()
            await asyncio.wait_for(provider_closed.wait(), timeout=1.5)
            await asyncio.wait_for(asyncio.shield(deferred), timeout=0.2)
            assert deferred.done() is True
            assert close_states == [True]
            assert hub._provider_runtime.provider_call_in_flight("slow", "stock") is False

            assert await hub.aclose(timeout=0.5) is True
            assert close_states == [True]
        finally:
            release.set()
            await hub.aclose(timeout=0.5)

    asyncio.run(run_check())


def test_datahub_concurrent_close_waiters_keep_independent_deadlines(tmp_path: Path) -> None:
    class BlockingRuntime:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def aclose(self, timeout: float) -> bool:
            self.started.set()
            await self.release.wait()
            return True

    async def run_check() -> None:
        hub = DataHub(settings=Settings(cache_path=tmp_path / "cache.sqlite3"))
        runtime = BlockingRuntime()
        hub._provider_runtime = runtime  # type: ignore[assignment]
        hub.providers = {}
        long_waiter = asyncio.create_task(hub.aclose(timeout=0.20))
        try:
            await asyncio.wait_for(runtime.started.wait(), timeout=0.1)
            shared_task = hub._provider_close_task
            started = asyncio.get_running_loop().time()

            assert await hub.aclose(timeout=0.01) is False

            elapsed = asyncio.get_running_loop().time() - started
            assert 0.008 <= elapsed < 0.08
            assert long_waiter.done() is False
            assert hub._provider_close_task is shared_task

            runtime.release.set()
            assert await long_waiter is True
        finally:
            runtime.release.set()
            await asyncio.gather(long_waiter, return_exceptions=True)

    asyncio.run(run_check())


def test_datahub_first_close_waiter_cancellation_does_not_cancel_shared_task(tmp_path: Path) -> None:
    provider_closed = asyncio.Event()

    class BlockingRuntime:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def aclose(self, timeout: float) -> bool:
            self.started.set()
            await self.release.wait()
            return True

    class CloseTrackingProvider:
        async def aclose(self) -> None:
            provider_closed.set()

    async def run_check() -> None:
        hub = DataHub(settings=Settings(cache_path=tmp_path / "cache.sqlite3"))
        runtime = BlockingRuntime()
        hub._provider_runtime = runtime  # type: ignore[assignment]
        hub.providers = {"tracked": CloseTrackingProvider()}
        first_waiter = asyncio.create_task(hub.aclose(timeout=0.5))
        try:
            await asyncio.wait_for(runtime.started.wait(), timeout=0.1)
            shared_task = hub._provider_close_task
            assert shared_task is not None

            first_waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_waiter

            assert hub._provider_close_task is shared_task
            assert shared_task.cancelled() is False

            runtime.release.set()
            assert await asyncio.wait_for(asyncio.shield(shared_task), timeout=0.2) is True
            assert provider_closed.is_set()
        finally:
            runtime.release.set()
            await asyncio.gather(first_waiter, return_exceptions=True)

    asyncio.run(run_check())


def test_datahub_runtime_close_retries_without_extra_sleep(tmp_path: Path) -> None:
    class SequencedRuntime:
        def __init__(self) -> None:
            self.calls = 0

        async def aclose(self, timeout: float) -> bool:
            self.calls += 1
            await asyncio.sleep(0.005)
            return self.calls >= 3

    async def run_check() -> None:
        hub = DataHub(settings=Settings(cache_path=tmp_path / "cache.sqlite3"))
        runtime = SequencedRuntime()
        hub._provider_runtime = runtime  # type: ignore[assignment]
        hub.providers = {}
        started = asyncio.get_running_loop().time()

        assert await hub.aclose(timeout=0.1) is True

        assert asyncio.get_running_loop().time() - started < 0.08
        assert runtime.calls == 3

    asyncio.run(run_check())


def test_datahub_provider_partial_failure_retries_only_failed_provider(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    close_calls = {"stable": 0, "flaky": 0}

    class StableProvider:
        async def aclose(self) -> None:
            close_calls["stable"] += 1

    class FlakyProvider:
        async def aclose(self) -> None:
            close_calls["flaky"] += 1
            if close_calls["flaky"] == 1:
                raise RuntimeError("/private/cache.sqlite3 api_key=do-not-log")

    async def run_check() -> None:
        hub = DataHub(settings=Settings(cache_path=tmp_path / "cache.sqlite3"))
        hub.providers = {
            "stable": StableProvider(),
            "flaky": FlakyProvider(),
        }

        assert await hub.aclose(timeout=0.2) is False
        assert hub._providers_closed is False
        assert close_calls == {"stable": 1, "flaky": 1}

        assert await hub.aclose(timeout=0.2) is True
        assert close_calls == {"stable": 1, "flaky": 2}
        assert await hub.aclose(timeout=0.2) is True
        assert close_calls == {"stable": 1, "flaky": 2}

    caplog.set_level(logging.WARNING, logger="app.services.datahub")
    asyncio.run(run_check())

    diagnostic = caplog.text
    assert "DataHub provider shutdown failed: RuntimeError" in diagnostic
    assert "cache.sqlite3" not in diagnostic
    assert "do-not-log" not in diagnostic
    assert "api_key" not in diagnostic


def test_datahub_shared_close_task_exception_is_consumed_and_visible_to_waiters(tmp_path: Path) -> None:
    loop_errors: list[dict] = []

    class FailingRuntime:
        async def aclose(self, timeout: float) -> bool:
            raise RuntimeError("runtime close failed")

    async def run_check() -> None:
        hub = DataHub(settings=Settings(cache_path=tmp_path / "cache.sqlite3"))
        hub._provider_runtime = FailingRuntime()  # type: ignore[assignment]
        hub.providers = {}
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            with pytest.raises(RuntimeError, match="runtime close failed"):
                await hub.aclose(timeout=0.1)
            await asyncio.sleep(0)
            assert loop_errors == []
        finally:
            loop.set_exception_handler(previous_handler)

    asyncio.run(run_check())


def test_datahub_provider_cancelled_error_propagates_and_retries_only_unfinished_provider(tmp_path: Path) -> None:
    close_calls = {"stable": 0, "cancelled": 0}

    class StableProvider:
        async def aclose(self) -> None:
            close_calls["stable"] += 1

    class CancelOnceProvider:
        async def aclose(self) -> None:
            close_calls["cancelled"] += 1
            if close_calls["cancelled"] == 1:
                raise asyncio.CancelledError

    async def run_check() -> None:
        hub = DataHub(settings=Settings(cache_path=tmp_path / "cache.sqlite3"))
        hub.providers = {
            "stable": StableProvider(),
            "cancelled": CancelOnceProvider(),
        }

        with pytest.raises(asyncio.CancelledError):
            await hub.aclose(timeout=0.2)

        assert close_calls == {"stable": 1, "cancelled": 1}
        assert hub._providers_closed is False
        assert await hub.aclose(timeout=0.2) is True
        assert close_calls == {"stable": 1, "cancelled": 2}

    asyncio.run(run_check())


def test_datahub_repeated_close_waiters_share_deferred_provider_close_and_survive_cancellation(tmp_path) -> None:
    release = threading.Event()
    started = threading.Event()
    provider_closed = asyncio.Event()
    close_calls = 0

    def blocking_call() -> str:
        started.set()
        release.wait(timeout=2)
        return "done"

    class CloseTrackingProvider:
        async def aclose(self) -> None:
            nonlocal close_calls
            close_calls += 1
            provider_closed.set()

    async def run_check() -> None:
        settings = Settings(
            cache_path=tmp_path / "cache.sqlite3",
            provider_call_timeout_seconds=0.01,
        )
        hub = DataHub(settings=settings)
        hub.providers = {"slow": CloseTrackingProvider()}
        try:
            with pytest.raises(ProviderCallTimeoutError):
                await hub._provider_runtime.call_provider(
                    "slow",
                    "stock",
                    lambda: run_provider_io(blocking_call),
                )
            assert started.is_set()
            assert await hub.aclose(timeout=0.01) is False
            deferred = hub._provider_close_task
            assert deferred is not None

            cancelled_waiter = asyncio.create_task(hub.aclose(timeout=0.5))
            second_waiter = asyncio.create_task(hub.aclose(timeout=0.5))
            await asyncio.sleep(0)
            cancelled_waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled_waiter
            assert hub._provider_close_task is deferred
            assert deferred.done() is False

            release.set()
            assert await second_waiter is True
            await asyncio.wait_for(provider_closed.wait(), timeout=1.5)
            assert close_calls == 1
            assert await hub.aclose(timeout=0.1) is True
            assert close_calls == 1
        finally:
            release.set()
            await hub.aclose(timeout=0.5)

    asyncio.run(run_check())


def test_datahub_cancelled_deferred_close_task_is_consumed_and_can_be_restarted(tmp_path) -> None:
    release = threading.Event()
    started = threading.Event()
    provider_closed = asyncio.Event()
    loop_errors: list[dict] = []

    def blocking_call() -> str:
        started.set()
        release.wait(timeout=2)
        return "done"

    class CloseTrackingProvider:
        async def aclose(self) -> None:
            provider_closed.set()

    async def run_check() -> None:
        settings = Settings(
            cache_path=tmp_path / "cache.sqlite3",
            provider_call_timeout_seconds=0.01,
        )
        hub = DataHub(settings=settings)
        hub.providers = {"slow": CloseTrackingProvider()}
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            with pytest.raises(ProviderCallTimeoutError):
                await hub._provider_runtime.call_provider(
                    "slow",
                    "stock",
                    lambda: run_provider_io(blocking_call),
                )
            assert started.is_set()
            assert await hub.aclose(timeout=0.01) is False
            deferred = hub._provider_close_task
            assert deferred is not None
            deferred.cancel()
            await asyncio.gather(deferred, return_exceptions=True)
            await asyncio.sleep(0)
            assert loop_errors == []

            release.set()
            assert await hub.aclose(timeout=0.5) is True
            await asyncio.wait_for(provider_closed.wait(), timeout=1.5)
        finally:
            loop.set_exception_handler(previous_handler)
            release.set()
            await hub.aclose(timeout=0.5)

    asyncio.run(run_check())


def test_provider_source_name_falls_back_for_blank_sources() -> None:
    assert provider_source_name(type("Provider", (), {"source_name": "  "})(), "fallback") == "fallback"
    assert provider_source_name(type("Provider", (), {"source_name": "实时源"})(), "fallback") == "实时源"


def test_provider_runtime_status_write_failures_do_not_escape_or_block_cooldown() -> None:
    cache = _FailingStatusCache()
    runtime = ProviderRuntime(cache, Settings(provider_failure_cooldown_seconds=30))

    runtime.record_success("broken", 1, 12.0, "quote")
    runtime.record_failure("broken", 1, RuntimeError("provider down"), "quote")

    assert runtime.is_cooling("broken", "quote") is True
    assert cache.success_calls == [("broken", "quote", 1, 12.0)]
    assert cache.failure_calls == [("broken", "quote", 1, "provider down")]


def test_provider_runtime_attempt_failure_uses_readable_error_for_blank_exception() -> None:
    cache = _FailingStatusCache()
    runtime = ProviderRuntime(cache, Settings(provider_failure_cooldown_seconds=30))
    errors: list[str] = []
    attempt = next(runtime.attempts([(1, "blank")], {"blank": object()}, "quote", errors))

    runtime.record_attempt_failure(attempt, "quote", RuntimeError(), errors)

    assert errors == ["blank: RuntimeError"]
    assert cache.failure_calls == [("blank", "quote", 1, "RuntimeError")]


def test_provider_runtime_coverage_miss_does_not_persist_failure_or_start_cooldown() -> None:
    cache = _FailingStatusCache()
    runtime = ProviderRuntime(cache, Settings(provider_failure_cooldown_seconds=30))
    errors: list[str] = []
    attempt = next(runtime.attempts([(1, "partial")], {"partial": object()}, "quote", errors))

    runtime.record_attempt_failure(
        attempt,
        "quote",
        ProviderCoverageMiss("未覆盖 688001.SH"),
        errors,
    )

    assert errors == ["partial: 未覆盖 688001.SH"]
    assert cache.failure_calls == []
    assert runtime.is_cooling("partial", "quote") is False


def test_provider_runtime_sanitizes_persisted_and_returned_error_text() -> None:
    cache = _FailingStatusCache()
    runtime = ProviderRuntime(cache, Settings(provider_failure_cooldown_seconds=30))
    errors: list[str] = []
    attempt = next(runtime.attempts([(1, "secure")], {"secure": object()}, "quote", errors))
    exc = RuntimeError("GET https://alice:secret@example.test/quote?token=raw-token&symbol=600519 failed")

    runtime.record_attempt_failure(attempt, "quote", exc, errors)

    persisted = cache.failure_calls[0][3]
    returned = errors[0]
    assert "alice" not in persisted + returned
    assert "secret" not in persisted + returned
    assert "raw-token" not in persisted + returned
    assert persisted.count("<redacted>") >= 2


def test_provider_runtime_redacts_naked_configured_credentials() -> None:
    tushare_token = "configured-tushare-token-value"
    llm_api_key = "configured-llm-key-value"
    cache = _FailingStatusCache()
    runtime = ProviderRuntime(
        cache,
        Settings(
            provider_failure_cooldown_seconds=30,
            tushare_token=tushare_token,
            llm_api_key=llm_api_key,
        ),
    )
    errors: list[str] = []
    attempt = next(runtime.attempts([(1, "secure")], {"secure": object()}, "quote", errors))

    runtime.record_attempt_failure(
        attempt,
        "quote",
        RuntimeError(f"{tushare_token} / {llm_api_key}"),
        errors,
    )

    output = cache.failure_calls[0][3] + errors[0]
    assert tushare_token not in output
    assert llm_api_key not in output
    assert output.count("<redacted>") >= 4


def test_provider_runtime_async_status_writes_run_off_event_loop_thread() -> None:
    class ThreadTrackingStatusCache(_FailingStatusCache):
        def __init__(self) -> None:
            super().__init__()
            self.io_threads: list[int] = []

        def update_provider_capability_success(
            self,
            name: str,
            kind: str,
            priority: int,
            latency_ms: float,
        ) -> None:
            self.io_threads.append(threading.get_ident())
            super().update_provider_capability_success(name, kind, priority, latency_ms)

        def update_provider_capability_failure(
            self,
            name: str,
            kind: str,
            priority: int,
            error: str,
        ) -> None:
            self.io_threads.append(threading.get_ident())
            super().update_provider_capability_failure(name, kind, priority, error)

    async def run_check() -> tuple[ThreadTrackingStatusCache, int, bool]:
        cache = ThreadTrackingStatusCache()
        runtime = ProviderRuntime(cache, Settings(provider_failure_cooldown_seconds=30))
        event_loop_thread = threading.get_ident()

        await runtime.record_success_async("secure", 1, 12.0, "quote")
        await runtime.record_failure_async(
            "secure",
            1,
            RuntimeError("GET https://alice:secret@example.test/quote?token=raw-token failed"),
            "quote",
        )
        return cache, event_loop_thread, runtime.is_cooling("secure", "quote")

    cache, event_loop_thread, cooling = asyncio.run(run_check())

    assert len(cache.io_threads) == 2
    assert all(thread_id != event_loop_thread for thread_id in cache.io_threads)
    assert cooling is True
    persisted_error = cache.failure_calls[0][3]
    assert "alice" not in persisted_error
    assert "secret" not in persisted_error
    assert "raw-token" not in persisted_error


class _FailingStatusCache:
    def __init__(self) -> None:
        self.success_calls: list[tuple[str, str, int, float]] = []
        self.failure_calls: list[tuple[str, str, int, str]] = []

    def update_provider_capability_success(self, name: str, kind: str, priority: int, latency_ms: float) -> None:
        self.success_calls.append((name, kind, priority, latency_ms))
        raise RuntimeError("status db readonly")

    def update_provider_capability_failure(self, name: str, kind: str, priority: int, error: str) -> None:
        self.failure_calls.append((name, kind, priority, error))
        raise RuntimeError("status db readonly")


async def _wait_for_provider_waiters(
    runtime: ProviderRuntime,
    name: str,
    kind: str,
    request_key: object,
    expected: int,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 0.5
    full_key = (name, kind, request_key)
    while loop.time() < deadline:
        state = runtime._provider_calls.get(full_key)
        if state is not None and state.waiters == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"provider waiter count did not reach {expected}")


async def _async_value(value: str) -> str:
    return value
