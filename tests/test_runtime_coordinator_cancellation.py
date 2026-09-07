from __future__ import annotations

import asyncio
import threading

import pytest

from app.services.runtime_coordinator import RuntimeCoordinator, RuntimeLeadership


class _Service:
    def __init__(self) -> None:
        self.running = False
        self.start_calls = 0
        self.stop_calls = 0

    @property
    def is_quiescent(self) -> bool:
        return not self.running

    async def start(self) -> bool:
        self.start_calls += 1
        self.running = True
        return True

    async def stop(self) -> bool:
        self.stop_calls += 1
        self.running = False
        return True


class _ThreadBarrierGuard:
    def __init__(self, *, block_release: bool = False, acquire_result: bool = True) -> None:
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.proceed = threading.Event()
        self.block_release = block_release
        self.acquire_result = acquire_result
        self.acquired = False

    def _wait(self) -> None:
        self.loop.call_soon_threadsafe(self.entered.set)
        if not self.proceed.wait(3):
            raise TimeoutError("test did not release the thread barrier")

    def acquire(self) -> bool:
        if not self.block_release:
            self._wait()
        self.acquired = self.acquire_result
        return self.acquired

    def release(self) -> None:
        if self.block_release:
            self._wait()
        self.acquired = False


@pytest.mark.parametrize("cancellation_count", [1, 3])
@pytest.mark.parametrize("acquire_result", [True, False])
def test_cancelled_start_drains_thread_acquisition_and_releases_lease(
    cancellation_count: int, acquire_result: bool
) -> None:
    async def scenario() -> None:
        guard = _ThreadBarrierGuard(acquire_result=acquire_result)
        leadership = RuntimeLeadership(guard)
        service = _Service()
        coordinator = RuntimeCoordinator(leadership, service, None)
        start = asyncio.create_task(coordinator.start())
        try:
            await asyncio.wait_for(guard.entered.wait(), timeout=1)
            for _ in range(cancellation_count):
                start.cancel()
                await asyncio.sleep(0)
            guard.proceed.set()
            with pytest.raises(asyncio.CancelledError):
                await start
            assert await asyncio.to_thread(lambda: leadership.is_leader) is False
            assert service.start_calls == 0
            assert guard.acquired is False
        finally:
            guard.proceed.set()
            await coordinator.stop()

    asyncio.run(scenario())


class _FailingReleaseGuard:
    def __init__(self) -> None:
        self.release_calls = 0
        self.acquire_calls = 0

    def acquire(self) -> bool:
        self.acquire_calls += 1
        return True

    def release(self) -> None:
        self.release_calls += 1
        if self.release_calls == 1:
            raise OSError("release failed")


@pytest.mark.parametrize("retry", ["stop", "start"])
def test_coordinator_retries_failed_release_before_reactivation(retry: str) -> None:
    async def scenario() -> None:
        guard = _FailingReleaseGuard()
        leadership = RuntimeLeadership(guard)
        scheduler = _Service()
        coordinator = RuntimeCoordinator(leadership, scheduler, None)
        assert await coordinator.start()
        with pytest.raises(OSError, match="release failed"):
            await coordinator.stop()
        assert leadership.needs_release and not leadership.is_leader
        if retry == "start":
            assert await coordinator.start()
            assert guard.release_calls == guard.acquire_calls == 2
        await coordinator.stop()
        assert not leadership.needs_release
        assert not scheduler.running

    asyncio.run(scenario())


class _FailingActivationService(_Service):
    def __init__(self) -> None:
        super().__init__()
        self.rollback_entered = asyncio.Event()
        self.rollback_proceed = asyncio.Event()

    async def start(self) -> bool:
        await super().start()
        raise RuntimeError("activation failed")

    async def stop(self) -> bool:
        self.rollback_entered.set()
        await self.rollback_proceed.wait()
        return await super().stop()


@pytest.mark.parametrize("cancellation_count", [0, 1, 3])
def test_activation_rollback_finishes_before_repeated_cancellation_returns(tmp_path, cancellation_count: int) -> None:
    async def scenario() -> None:
        leadership = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
        scheduler, scanner = _FailingActivationService(), _Service()
        coordinator = RuntimeCoordinator(leadership, scheduler, scanner)
        start = asyncio.create_task(coordinator.start())
        try:
            await asyncio.wait_for(scheduler.rollback_entered.wait(), timeout=1)
            for _ in range(cancellation_count):
                start.cancel()
                await asyncio.sleep(0)
            assert not start.done()
        finally:
            scheduler.rollback_proceed.set()
            outcomes = await asyncio.gather(start, return_exceptions=True)
        expected = asyncio.CancelledError if cancellation_count else RuntimeError
        assert isinstance(outcomes[0], expected)
        assert not scanner.running and not scheduler.running
        assert not leadership.needs_release
        assert scheduler.stop_calls == scanner.stop_calls == 1

    asyncio.run(scenario())


def test_service_stop_cancellation_releases_quiescent_lease_without_wait_loop(tmp_path) -> None:
    class Service(_Service):
        async def stop(self) -> bool:
            await super().stop()
            raise asyncio.CancelledError

    async def scenario() -> None:
        leadership = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
        coordinator = RuntimeCoordinator(leadership, Service(), None)
        assert await coordinator.start()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(coordinator.stop(), timeout=1)
        assert not leadership.needs_release

    asyncio.run(scenario())


@pytest.mark.parametrize("cancellation_count", [1, 3])
def test_cancelled_stop_waits_for_thread_release(cancellation_count: int) -> None:
    async def scenario() -> None:
        guard = _ThreadBarrierGuard(block_release=True)
        leadership = RuntimeLeadership(guard)
        service = _Service()
        coordinator = RuntimeCoordinator(leadership, service, None)
        assert await coordinator.start()
        stop = asyncio.create_task(coordinator.stop())
        try:
            await asyncio.wait_for(guard.entered.wait(), timeout=1)
            for _ in range(cancellation_count):
                stop.cancel()
                await asyncio.sleep(0)
            assert not stop.done()
        finally:
            guard.proceed.set()
            await asyncio.gather(stop, return_exceptions=True)
        assert stop.cancelled()
        assert not leadership.is_leader
        assert not service.running
        assert not guard.acquired

    asyncio.run(scenario())


def test_stop_serializes_runner_selection_against_concurrent_restart(tmp_path) -> None:
    async def scenario() -> None:
        leadership = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
        service = _Service()
        coordinator = RuntimeCoordinator(leadership, service, None, takeover_poll_seconds=0.05)
        assert await coordinator.start()
        async with coordinator._lifecycle_lock:  # noqa: SLF001
            restart = asyncio.create_task(coordinator.start())
            await asyncio.sleep(0)
            stop = asyncio.create_task(coordinator.stop())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        try:
            await asyncio.gather(restart, stop)
            await asyncio.sleep(0.15)
            assert service.running is False
            assert leadership.is_leader is False
            assert service.start_calls == 1
        finally:
            await coordinator.stop()

    asyncio.run(scenario())


def test_leadership_release_failure_disables_service_lease_but_preserves_retry() -> None:
    class Guard:
        def __init__(self) -> None:
            self.calls = 0

        def acquire(self) -> bool:
            return True

        def release(self) -> None:
            self.calls += 1
            if self.calls == 1:
                raise OSError("release failed")

    guard = Guard()
    leadership = RuntimeLeadership(guard)
    assert leadership.try_acquire()
    with pytest.raises(OSError, match="release failed"):
        leadership.release()
    assert not leadership.is_leader
    assert not leadership.service_guard().acquire()
    assert leadership.needs_release
    leadership.release()
    assert not leadership.is_leader
    assert not leadership.needs_release
    assert guard.calls == 2


def test_stop_interrupts_standby_activation_waiting_inside_service_start(tmp_path) -> None:
    class Service(_Service):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.proceed = asyncio.Event()

        async def start(self) -> bool:
            self.entered.set()
            try:
                await self.proceed.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            return await super().start()

    async def scenario() -> None:
        leadership = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
        holder = RuntimeLeadership.for_cache_path(tmp_path / "cache.sqlite3")
        service = Service()
        coordinator = RuntimeCoordinator(leadership, service, None, takeover_poll_seconds=0.05)
        assert holder.try_acquire()
        assert not await coordinator.start()
        holder.release()
        await asyncio.wait_for(service.entered.wait(), timeout=1)
        stop = asyncio.create_task(coordinator.stop())
        try:
            await asyncio.wait_for(service.cancelled.wait(), timeout=0.2)
        finally:
            service.proceed.set()
            await stop
        assert not leadership.needs_release
        assert not service.running

    asyncio.run(scenario())
