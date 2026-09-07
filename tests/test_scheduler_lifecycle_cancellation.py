from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.scheduler_service import LocalDataScheduler
from tests.test_runtime_coordinator_cancellation import _ThreadBarrierGuard


def _scheduler(guard: _ThreadBarrierGuard) -> LocalDataScheduler:
    settings = SimpleNamespace(
        scheduler_enabled=True,
        scheduler_shutdown_timeout_seconds=0.01,
        scheduler_quote_interval_seconds=3600,
        scheduler_kline_interval_seconds=3600,
        scheduler_plate_interval_seconds=3600,
        scheduler_health_interval_seconds=3600,
    )
    cache = SimpleNamespace(
        reconcile_orphaned_task_runs=lambda: 0,
        save_monitor_event=lambda *_args, **_kwargs: None,
    )
    scheduler = LocalDataScheduler(SimpleNamespace(settings=settings, cache=cache), instance_guard=guard)
    for task in scheduler.tasks.values():
        task.next_run_at = datetime.now() + timedelta(days=1)
    return scheduler


@pytest.mark.parametrize("acquire_result", [True, False])
def test_scheduler_repeated_start_cancellation_releases_eventual_acquisition(acquire_result: bool) -> None:
    async def scenario() -> None:
        guard = _ThreadBarrierGuard(acquire_result=acquire_result)
        scheduler = _scheduler(guard)
        start = asyncio.create_task(scheduler.start())
        try:
            await asyncio.wait_for(guard.entered.wait(), timeout=1)
            for _ in range(3):
                start.cancel()
                await asyncio.sleep(0)
            assert not start.done()
        finally:
            guard.proceed.set()
            await asyncio.gather(start, return_exceptions=True)
        assert start.cancelled()
        assert not guard.acquired
        assert scheduler.is_quiescent

    asyncio.run(scenario())


def test_scheduler_repeated_stop_cancellation_drains_release_before_returning() -> None:
    async def scenario() -> None:
        guard = _ThreadBarrierGuard(block_release=True)
        scheduler = _scheduler(guard)
        assert await scheduler.start()
        stop = asyncio.create_task(scheduler.stop())
        try:
            await asyncio.wait_for(guard.entered.wait(), timeout=1)
            for _ in range(3):
                stop.cancel()
                await asyncio.sleep(0)
            assert not stop.done()
        finally:
            guard.proceed.set()
            await asyncio.gather(stop, return_exceptions=True)
        assert stop.cancelled()
        assert not guard.acquired
        assert scheduler.is_quiescent

    asyncio.run(scenario())


def test_manual_guard_release_cancellation_does_not_restore_a_released_guard() -> None:
    async def scenario() -> None:
        guard = _ThreadBarrierGuard(block_release=True)
        scheduler = _scheduler(guard)
        assert await scheduler._begin_manual_guard_use()  # noqa: SLF001
        release = asyncio.create_task(scheduler._end_manual_guard_use())  # noqa: SLF001
        try:
            await asyncio.wait_for(guard.entered.wait(), timeout=1)
            for _ in range(3):
                release.cancel()
                await asyncio.sleep(0)
            assert not release.done()
        finally:
            guard.proceed.set()
            await asyncio.gather(release, return_exceptions=True)
        assert release.cancelled()
        assert not guard.acquired
        assert scheduler.is_quiescent
        await asyncio.wait_for(scheduler.wait_until_quiescent(), timeout=1)

    asyncio.run(scenario())


def test_scheduler_start_rollback_is_not_interrupted_by_repeated_cancellation() -> None:
    def failed_reconciliation() -> None:
        raise RuntimeError("reconcile failed")

    async def scenario() -> None:
        guard = _ThreadBarrierGuard(block_release=True)
        scheduler = _scheduler(guard)
        scheduler.datahub.cache.reconcile_orphaned_task_runs = failed_reconciliation
        start = asyncio.create_task(scheduler.start())
        try:
            await asyncio.wait_for(guard.entered.wait(), timeout=1)
            for _ in range(3):
                start.cancel()
                await asyncio.sleep(0)
            assert not start.done()
        finally:
            guard.proceed.set()
            await asyncio.gather(start, return_exceptions=True)
        assert start.cancelled()
        assert not guard.acquired
        assert scheduler.is_quiescent
        assert scheduler.started_at is None

    asyncio.run(scenario())


class _ReleasedThenFailedGuard(_ThreadBarrierGuard):
    def __init__(self) -> None:
        super().__init__(block_release=True)
        self.proceed.set()
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self) -> bool:
        self.acquire_calls += 1
        return super().acquire()

    def release(self) -> None:
        self.release_calls += 1
        super().release()
        if self.release_calls == 1:
            raise OSError("released handle but unlock reporting failed")


@pytest.mark.parametrize("retry", ["start", "manual", "stop"])
def test_scheduler_never_reuses_ownership_after_release_raises(retry: str) -> None:
    async def scenario() -> None:
        guard = _ReleasedThenFailedGuard()
        scheduler = _scheduler(guard)
        assert await scheduler.start()
        with pytest.raises(OSError, match="unlock reporting failed"):
            await scheduler.stop()
        assert not guard.acquired
        assert not scheduler.is_quiescent
        if retry == "start":
            assert await scheduler.start()
            assert guard.acquired and guard.acquire_calls == 2
        elif retry == "manual":
            assert await scheduler._begin_manual_guard_use()  # noqa: SLF001
            assert guard.acquired and guard.acquire_calls == 2
            await scheduler._end_manual_guard_use()  # noqa: SLF001
        await scheduler.stop()
        assert scheduler.is_quiescent and not guard.acquired
        assert guard.release_calls >= 2

    asyncio.run(scenario())


def test_manual_run_repeated_cancellation_drains_cleanup_waiting_for_lifecycle_lock(monkeypatch) -> None:
    async def scenario() -> None:
        guard = _ThreadBarrierGuard(block_release=True)
        guard.proceed.set()
        scheduler = _scheduler(guard)
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def execute(_task, *, manual: bool) -> str:
            assert manual
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "unreachable"

        monkeypatch.setattr(scheduler, "_execute", execute)
        run = asyncio.create_task(scheduler.run_once("refresh_watch_quotes"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        async with scheduler._lifecycle_lock:  # noqa: SLF001
            run.cancel()
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            for _ in range(2):
                run.cancel()
                await asyncio.sleep(0)
            assert not run.done()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, timeout=1)
        assert scheduler._manual_guard_users == 0  # noqa: SLF001
        assert not guard.acquired
        assert scheduler.is_quiescent
        await scheduler.stop()
        await asyncio.wait_for(scheduler.wait_until_quiescent(), timeout=1)

    asyncio.run(scenario())
