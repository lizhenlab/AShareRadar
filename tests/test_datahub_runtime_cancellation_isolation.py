from __future__ import annotations

import asyncio
from concurrent.futures import Future
import threading

import pytest

from app.config import Settings
from app.services.daemon_executor import DaemonThreadPoolExecutor
from app.services.datahub_runtime import ProviderCallBusyError, ProviderCallTimeoutError, ProviderRuntime, await_provider_worker


async def _depart(caller: asyncio.Task, departure: str) -> None:
    if departure == "cancel":
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
    else:
        with pytest.raises(ProviderCallTimeoutError):
            await caller


async def _ready_value():
    return "ready"


@pytest.mark.parametrize("departure", ["cancel", "timeout"])
def test_late_same_key_caller_cannot_join_a_cancelling_request(departure):
    async def check():
        runtime = ProviderRuntime(object(), Settings(provider_call_timeout_seconds=1))
        started, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def fetch():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await release.wait()
                raise

        timeout = .2 if departure == "timeout" else 1
        first = asyncio.create_task(runtime.call_provider("fake", "quote", fetch, request_key="same", timeout_seconds=timeout))
        late = None
        try:
            await asyncio.wait_for(started.wait(), 1)
            if departure == "cancel":
                first.cancel()
            await asyncio.wait_for(cancelling.wait(), 1)
            late = asyncio.create_task(runtime.call_provider("fake", "quote", fetch, request_key="same"))
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(ProviderCallBusyError, match="取消"):
                await late
            assert late.cancelling() == 0
            await _depart(first, departure)

            async def replacement():
                return "new request"

            assert await runtime.call_provider("fake", "quote", replacement, request_key="same") == "new request"
        finally:
            release.set()
            await asyncio.gather(*(task for task in (first, late) if task is not None), return_exceptions=True)
            await runtime.aclose()

    asyncio.run(check())


class _SerialWorkers:
    def __init__(self):
        self.executor = DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix="cancel-isolation-test")
        self.started, self.release = threading.Event(), threading.Event()
        self.submitted = asyncio.Event()
        self.executed: list[str] = []
        self.futures: dict[str, Future] = {}

    def work(self, name):
        self.executed.append(name)
        if name == "active":
            self.started.set()
            if not self.release.wait(2):
                raise RuntimeError("test worker release timed out")
        return name

    async def fetch(self, name):
        worker = self.executor.submit(self.work, name)
        self.futures[name] = worker
        self.submitted.set()
        return await await_provider_worker(worker)

    def close(self):
        self.release.set()
        self.executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("departure", ["cancel", "timeout"])
def test_last_waiter_departure_cancels_queued_sdk_work_before_it_starts(departure):
    async def check():
        runtime = ProviderRuntime(object(), Settings(provider_call_timeout_seconds=1))
        workers = _SerialWorkers()
        active = asyncio.create_task(runtime.call_provider("fake", "kline", lambda: workers.fetch("active"), request_key="active"))
        queued = None
        try:
            assert await asyncio.to_thread(workers.started.wait, 1)
            workers.submitted.clear()
            timeout = .2 if departure == "timeout" else 1
            queued = asyncio.create_task(runtime.call_provider(
                "fake", "kline", lambda: workers.fetch("abandoned"), request_key="queued", timeout_seconds=timeout,
            ))
            await asyncio.wait_for(workers.submitted.wait(), 1)
            assert not workers.futures["abandoned"].running()
            await _depart(queued, departure)
            assert workers.futures["abandoned"].cancelled()
            assert runtime.provider_call_in_flight("fake", "kline")
            assert await runtime.call_provider("fake", "kline", _ready_value, request_key="fresh") == "ready"
            workers.release.set()
            assert await active == "active"
            assert workers.executed == ["active"]
        finally:
            workers.release.set()
            await asyncio.gather(*(task for task in (active, queued) if task is not None), return_exceptions=True)
            await runtime.aclose()
            workers.close()

    asyncio.run(check())


@pytest.mark.parametrize("departure", ["cancel", "timeout"])
def test_shared_queued_worker_survives_one_waiter_departure(departure):
    async def check():
        runtime = ProviderRuntime(object(), Settings(provider_call_timeout_seconds=1))
        workers = _SerialWorkers()
        active = asyncio.create_task(runtime.call_provider("fake", "kline", lambda: workers.fetch("active"), request_key="active"))
        first = second = None
        try:
            assert await asyncio.to_thread(workers.started.wait, 1)
            workers.submitted.clear()
            timeout = .2 if departure == "timeout" else 1
            first = asyncio.create_task(runtime.call_provider(
                "fake", "kline", lambda: workers.fetch("shared"), request_key="shared", timeout_seconds=timeout,
            ))
            await asyncio.wait_for(workers.submitted.wait(), 1)
            second = asyncio.create_task(runtime.call_provider("fake", "kline", lambda: workers.fetch("shared"), request_key="shared"))
            await asyncio.sleep(0)
            await _depart(first, departure)
            assert not workers.futures["shared"].cancelled()
            assert not second.done()
            workers.release.set()
            assert await second == "shared"
            assert await active == "active"
            assert workers.executed == ["active", "shared"]
        finally:
            workers.release.set()
            await asyncio.gather(*(task for task in (active, first, second) if task is not None), return_exceptions=True)
            await runtime.aclose()
            workers.close()

    asyncio.run(check())


def test_last_shared_waiter_cancels_queued_worker():
    async def check():
        runtime = ProviderRuntime(object(), Settings(provider_call_timeout_seconds=1))
        workers = _SerialWorkers()
        active = asyncio.create_task(runtime.call_provider("fake", "kline", lambda: workers.fetch("active"), request_key="active"))
        first = second = None
        try:
            assert await asyncio.to_thread(workers.started.wait, 1)
            workers.submitted.clear()
            first = asyncio.create_task(runtime.call_provider("fake", "kline", lambda: workers.fetch("abandoned"), request_key="same"))
            await asyncio.wait_for(workers.submitted.wait(), 1)
            second = asyncio.create_task(runtime.call_provider("fake", "kline", lambda: workers.fetch("abandoned"), request_key="same"))
            await asyncio.sleep(0)
            await _depart(first, "cancel")
            assert not workers.futures["abandoned"].cancelled()
            await _depart(second, "cancel")
            assert workers.futures["abandoned"].cancelled()
            workers.release.set()
            assert await active == "active"
            assert workers.executed == ["active"]
        finally:
            workers.release.set()
            await asyncio.gather(*(task for task in (active, first, second) if task is not None), return_exceptions=True)
            await runtime.aclose()
            workers.close()

    asyncio.run(check())


def test_unrelated_running_worker_does_not_prevent_async_request_cancellation():
    async def check():
        runtime = ProviderRuntime(object(), Settings(provider_call_timeout_seconds=1))
        workers = _SerialWorkers()
        active = asyncio.create_task(runtime.call_provider("fake", "kline", lambda: workers.fetch("active"), request_key="active"))
        started, cancelled = asyncio.Event(), asyncio.Event()
        caller = None

        async def asynchronous_fetch():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        try:
            assert await asyncio.to_thread(workers.started.wait, 1)
            caller = asyncio.create_task(runtime.call_provider("fake", "kline", asynchronous_fetch, request_key="async"))
            await asyncio.wait_for(started.wait(), 1)
            await _depart(caller, "cancel")
            assert cancelled.is_set()
            assert workers.futures["active"].running()
            assert runtime.provider_call_in_flight("fake", "kline")
        finally:
            workers.release.set()
            await asyncio.gather(*(task for task in (active, caller) if task is not None), return_exceptions=True)
            await runtime.aclose()
            workers.close()

    asyncio.run(check())


def test_mixed_call_cancels_queued_work_but_tracks_running_thread_after_task_ends():
    async def check():
        runtime = ProviderRuntime(object(), Settings(provider_call_timeout_seconds=1))
        workers = _SerialWorkers()
        queued = asyncio.Event()

        async def mixed_fetch():
            first = asyncio.create_task(workers.fetch("active"))
            await asyncio.to_thread(workers.started.wait, 1)
            second = asyncio.create_task(workers.fetch("abandoned"))
            await asyncio.sleep(0)
            queued.set()
            return await asyncio.gather(first, second)

        caller = asyncio.create_task(runtime.call_provider("fake", "kline", mixed_fetch, request_key="mixed"))
        try:
            await asyncio.wait_for(queued.wait(), 1)
            await _depart(caller, "cancel")
            assert workers.futures["abandoned"].cancelled()
            assert workers.futures["active"].running()
            assert runtime.provider_call_in_flight("fake", "kline")
            with pytest.raises(ProviderCallBusyError, match="后台|取消"):
                await runtime.call_provider("fake", "kline", lambda: workers.fetch("replacement"), request_key="mixed")
            with pytest.raises(ProviderCallBusyError, match="后台"):
                await runtime.call_provider("fake", "kline", lambda: workers.fetch("replacement"), request_key="different")
            assert await runtime.aclose(timeout=0) is False
            workers.release.set()
            assert await runtime.aclose(timeout=1) is True
            assert not runtime.provider_call_in_flight("fake", "kline")
            assert workers.executed == ["active"]
        finally:
            workers.release.set()
            await asyncio.gather(caller, return_exceptions=True)
            await runtime.aclose()
            workers.close()

    asyncio.run(check())


@pytest.mark.parametrize("departure", ["cancel", "timeout"])
def test_running_sdk_call_can_still_be_rejoined_without_duplicate_work(departure):
    async def check():
        runtime = ProviderRuntime(object(), Settings(provider_call_timeout_seconds=1))
        workers = _SerialWorkers()
        timeout = .2 if departure == "timeout" else 1
        first = asyncio.create_task(runtime.call_provider(
            "fake", "kline", lambda: workers.fetch("active"), request_key="same", timeout_seconds=timeout,
        ))
        second = None
        try:
            assert await asyncio.to_thread(workers.started.wait, 1)
            await _depart(first, departure)
            assert workers.futures["active"].running()
            second = asyncio.create_task(runtime.call_provider("fake", "kline", lambda: workers.fetch("duplicate"), request_key="same"))
            await asyncio.sleep(0)
            workers.release.set()
            assert await second == "active"
            assert workers.executed == ["active"]
            assert not runtime.provider_call_in_flight("fake", "kline")
        finally:
            workers.release.set()
            await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
            await runtime.aclose()
            workers.close()

    asyncio.run(check())
