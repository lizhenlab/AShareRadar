from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.services.instance_guard import FileInstanceGuard
from app.services.scheduler_contracts import LocalTask
from app.services.scheduler_service import LocalDataScheduler
from app.utils.clock import market_now_naive
from tests.test_scheduler_modules import _SchedulerHub


class _ControlledScanner:
    def __init__(self, *, stubborn=False, fails=False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.exited = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.maximum_active = 0
        self.stubborn = stubborn
        self.fails = fails

    async def scheduled_tick(self, _now=None):
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.entered.set()
        try:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    if not self.stubborn:
                        raise
            if self.fails:
                raise RuntimeError("controlled preflight failure")
        finally:
            self.active -= 1
            self.exited.set()


def _scheduler(tmp_path, scanner):
    hub = _SchedulerHub()
    hub.settings.scheduler_enabled = True
    hub.settings.scheduler_shutdown_timeout_seconds = 0.02
    scheduler = LocalDataScheduler(hub, instance_guard=FileInstanceGuard(tmp_path / "scheduler.lock"), market_scanner=scanner)
    scheduler.tasks = {}
    return scheduler


def test_pending_tick_does_not_block_current_or_later_due_tasks_or_overlap(tmp_path):
    async def scenario():
        scanner = _ControlledScanner()
        scheduler = _scheduler(tmp_path, scanner)
        first, later = asyncio.Event(), asyncio.Event()
        counts = []
        async def handler(event, name):
            counts.append(name)
            event.set()
            return name
        now = market_now_naive()
        scheduler.tasks = {
            "first": LocalTask("first", "已到期", 600, lambda: handler(first, "first"), now-timedelta(seconds=1)),
            "later": LocalTask("later", "随后到期", 600, lambda: handler(later, "later"), now+timedelta(days=1)),
        }
        try:
            assert await scheduler.start()
            await asyncio.wait_for(scanner.entered.wait(), 1)
            await asyncio.wait_for(first.wait(), 1)
            assert not later.is_set()
            scheduler.tasks["later"].next_run_at = market_now_naive()
            await asyncio.wait_for(later.wait(), 1.5)
            assert scanner.calls == scanner.maximum_active == 1
            assert not scanner.release.is_set()
            assert counts == ["first", "later"]
        finally:
            scanner.release.set()
            await scheduler.stop()
            await asyncio.wait_for(scheduler.wait_until_quiescent(), 1)
    asyncio.run(scenario())


@pytest.mark.parametrize("fails", [False, True])
def test_completed_or_failed_tick_is_reaped_and_next_tick_can_run(tmp_path, fails):
    async def scenario():
        scanner = _ControlledScanner(fails=fails)
        scanner.release.set()
        scheduler = _scheduler(tmp_path, scanner)
        loop_errors = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            assert await scheduler.start()
            async with asyncio.timeout(2):
                while scanner.calls < 2:
                    await asyncio.sleep(0.005)
            assert scanner.maximum_active == 1
            assert scheduler._runner is not None and not scheduler._runner.done()  # noqa: SLF001
            if fails:
                assert any("controlled preflight failure" in event[2] for event in scheduler.datahub.cache.monitor_events)
        finally:
            await scheduler.stop()
            await asyncio.wait_for(scheduler.wait_until_quiescent(), 1)
        assert not loop_errors
    asyncio.run(scenario())


@pytest.mark.parametrize("stubborn", [False, True])
def test_stop_and_restart_keep_pending_tick_resources_owned(tmp_path, stubborn):
    async def scenario():
        scanner = _ControlledScanner(stubborn=stubborn)
        scheduler = _scheduler(tmp_path, scanner)
        competitor = FileInstanceGuard(tmp_path / "scheduler.lock")
        try:
            assert await scheduler.start()
            await asyncio.wait_for(scanner.entered.wait(), 1)
            assert await asyncio.wait_for(scheduler.stop(), 0.5)
            await asyncio.wait_for(scanner.cancelled.wait(), 1)
            if stubborn:
                assert not scanner.exited.is_set()
                assert not scheduler.is_quiescent
                assert not competitor.acquire()
                assert await scheduler.start() is False
            scanner.release.set()
            await asyncio.wait_for(scheduler.wait_until_quiescent(), 1)
            assert scanner.exited.is_set()
            assert competitor.acquire()
            competitor.release()
            previous_calls = scanner.calls
            assert await scheduler.start()
            async with asyncio.timeout(1):
                while scanner.calls == previous_calls:
                    await asyncio.sleep(0.005)
            assert scanner.maximum_active == 1
        finally:
            scanner.release.set()
            await scheduler.stop()
            await asyncio.wait_for(scheduler.wait_until_quiescent(), 1)
            competitor.release()
    asyncio.run(scenario())
