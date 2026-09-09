from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.instance_guard import FileInstanceGuard
from app.services.scheduler_helpers import _offload
from app.services.scheduler_service import LocalDataScheduler
from tests.test_scheduler_modules import _SchedulerCache


class _BlockingAutomation:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.effects: list[str] = []

    def run_due(self):
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("test worker was not released")
        self.effects.append("completed")
        self.finished.set()
        return SimpleNamespace(
            checked_count=1, executed_count=1, skipped_count=0,
            event_count=1, failed_count=0,
        )


def _scheduler(tmp_path: Path, service: _BlockingAutomation, *, enabled: bool):
    settings = Settings(
        cache_path=tmp_path / "unused.sqlite3", scheduler_enabled=enabled,
        scheduler_shutdown_timeout_seconds=0.02,
    )
    cache = _SchedulerCache()
    guard = FileInstanceGuard(tmp_path / "scheduler.lock")
    scheduler = LocalDataScheduler(
        SimpleNamespace(settings=settings, cache=cache),  # type: ignore[arg-type]
        instance_guard=guard, strategy_automation_service=service,
    )
    task = scheduler.tasks["run_strategy_schedules"]
    task.next_run_at = datetime(2020, 1, 1)
    scheduler.tasks = {task.name: task}
    return scheduler, cache


def test_manual_repeated_cancellation_keeps_worker_and_instance_guard_owned(tmp_path) -> None:
    async def check() -> None:
        service = _BlockingAutomation()
        scheduler, cache = _scheduler(tmp_path, service, enabled=False)
        competitor = FileInstanceGuard(tmp_path / "scheduler.lock")
        run = asyncio.create_task(scheduler.run_once("run_strategy_schedules"))
        try:
            assert await asyncio.to_thread(service.entered.wait, 1)
            for _ in range(3):
                run.cancel()
                await asyncio.sleep(0.01)
            assert not run.done()
            assert not scheduler.is_quiescent
            assert scheduler.tasks["run_strategy_schedules"].running
            assert not competitor.acquire()
            assert cache.finished_runs == []
        finally:
            service.release.set()
            await asyncio.gather(run, return_exceptions=True)
            competitor.release()
        assert run.cancelled()
        assert service.finished.is_set()
        assert scheduler.is_quiescent
        assert len(cache.finished_runs) == 1
        assert cache.finished_runs[0][0] == "cancelled"
        assert competitor.acquire()
        competitor.release()
    asyncio.run(check())


def test_stop_is_bounded_but_guard_and_quiescence_wait_for_sync_worker(tmp_path) -> None:
    async def check() -> None:
        service = _BlockingAutomation()
        scheduler, cache = _scheduler(tmp_path, service, enabled=True)
        competitor = FileInstanceGuard(tmp_path / "scheduler.lock")
        try:
            assert await scheduler.start()
            assert await asyncio.to_thread(service.entered.wait, 1)
            assert await asyncio.wait_for(scheduler.stop(), timeout=0.5)
            await asyncio.sleep(0.02)
            assert not service.finished.is_set()
            assert not scheduler.is_quiescent
            assert not competitor.acquire()
            assert await scheduler.start() is False
        finally:
            service.release.set()
            await asyncio.wait_for(scheduler.wait_until_quiescent(), timeout=1)
            competitor.release()
        assert service.finished.is_set()
        assert len(cache.finished_runs) == 1
        assert cache.finished_runs[0][0] == "cancelled"
        assert competitor.acquire()
        competitor.release()
    asyncio.run(check())


@pytest.mark.parametrize("fails", (False, True))
def test_offload_drains_late_worker_result_or_failure_before_cancellation(fails) -> None:
    async def check() -> None:
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def worker() -> str:
            entered.set()
            assert release.wait(5)
            finished.set()
            if fails:
                raise RuntimeError("late worker failure")
            return "done"
        run = asyncio.create_task(_offload(worker))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            run.cancel()
            await asyncio.sleep(0.01)
            run.cancel()
            await asyncio.sleep(0.01)
            assert not run.done()
        finally:
            release.set()
            await asyncio.gather(run, return_exceptions=True)
        assert run.cancelled()
        assert finished.is_set()
    asyncio.run(check())


@pytest.mark.parametrize("cancel", (False, True))
def test_health_read_group_keeps_guard_until_every_worker_finishes(tmp_path, cancel) -> None:
    async def check() -> None:
        service = _BlockingAutomation()
        scheduler, cache = _scheduler(tmp_path, service, enabled=False)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        failure_ready = threading.Event()
        def statistics():
            entered.set()
            assert release.wait(5)
            finished.set()
            return None
        def capabilities():
            assert entered.wait(1)
            failure_ready.set()
            if not cancel:
                raise RuntimeError("capability snapshot failed")
            return []
        cache.stats = statistics
        cache.provider_capability_statuses = capabilities
        cache.provider_statuses = lambda: []
        task = SimpleNamespace(
            name="check_data_health", display_name="健康检查", running=False,
            handler=scheduler._check_data_health, interval_seconds=600,
            next_run_at=datetime(2020, 1, 1),
        )
        scheduler.tasks = {task.name: task}
        competitor = FileInstanceGuard(tmp_path / "scheduler.lock")
        run = asyncio.create_task(scheduler.run_once(task.name))
        try:
            assert await asyncio.to_thread(failure_ready.wait, 1)
            if cancel:
                run.cancel()
            await asyncio.sleep(0.03)
            assert not run.done()
            assert not scheduler.is_quiescent
            assert not competitor.acquire()
        finally:
            release.set()
            await asyncio.gather(run, return_exceptions=True)
            competitor.release()
        assert finished.is_set()
        assert scheduler.is_quiescent
        if cancel:
            assert run.cancelled()
        else:
            assert isinstance(run.exception(), RuntimeError)
            assert str(run.exception()) == "capability snapshot failed"
        assert len(cache.finished_runs) == 1
    asyncio.run(check())
