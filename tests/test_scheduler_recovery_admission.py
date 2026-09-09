from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from app.api.deps import get_datahub, get_scheduler
from app.api.routes.monitoring import router
from app.repositories.runtime import RuntimeEventRepository
from app.services.instance_guard import FileInstanceGuard
from app.services.runtime_coordinator import RuntimeCoordinator, RuntimeLeadership
from app.services.scheduler_contracts import LocalTask
from app.services.scheduler_service import LocalDataScheduler
from tests.test_scheduler_modules import _SchedulerHub


def _runtime_hub(path: Path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE task_run (id INTEGER PRIMARY KEY AUTOINCREMENT, task_name TEXT NOT NULL,
                status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, duration_ms INTEGER, message TEXT);
            CREATE TABLE monitor_event (id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL,
                category TEXT NOT NULL, symbol TEXT, message TEXT NOT NULL, created_at TEXT NOT NULL,
                last_seen_at TEXT, repeat_count INTEGER DEFAULT 1);
        """)
    repo = RuntimeEventRepository(path, threading.RLock())
    cache = SimpleNamespace(
        path=path, start_task_run=repo.start_task_run, finish_task_run=repo.finish_task_run,
        recent_task_runs=repo.task_runs, reconcile_orphaned_task_runs=repo.reconcile_orphaned_task_runs,
        save_monitor_event=repo.save_monitor_event,
    )
    hub = _SchedulerHub(cache=cache)
    hub.settings.scheduler_enabled = True
    hub.settings.scheduler_shutdown_timeout_seconds = 0.02
    return hub


def _manual_task(scheduler, handler):
    name = "refresh_watch_quotes"
    scheduler.tasks = {name: LocalTask(name, "受控任务", 600, handler, datetime.now() + timedelta(days=1))}
    return name


def _client(hub, scheduler):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_datahub] = lambda: hub
    app.dependency_overrides[get_scheduler] = lambda: scheduler
    return AsyncClient(transport=ASGITransport(app), base_url="http://isolated")


@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
@pytest.mark.parametrize("stop_first", [False, True])
def test_start_does_not_reconcile_owned_manual_run_and_later_cleans_real_orphans(tmp_path, outcome, stop_first):
    async def scenario():
        hub = _runtime_hub(tmp_path / "runtime.sqlite3")
        scheduler = LocalDataScheduler(hub)
        competitor = FileInstanceGuard(Path(f"{hub.cache.path}.scheduler.lock"))
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler():
            entered.set()
            await release.wait()
            if outcome == "failed":
                raise RuntimeError("controlled failure")
            return "controlled success"
        name = _manual_task(scheduler, handler)
        if stop_first:
            assert await scheduler.start()
        async with _client(hub, scheduler) as client:
            request = asyncio.create_task(client.post("/api/tasks/run-once", params={"task": name}))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                if stop_first:
                    assert await scheduler.stop()
                assert not competitor.acquire()
                assert await scheduler.start() is False
                rows = (await client.get("/api/tasks/runs")).json()
                assert len(rows) == 1 and rows[0]["status"] == "running"
                if outcome == "cancelled":
                    request.cancel()
                release.set()
                result = (await asyncio.gather(request, return_exceptions=True))[0]
                if outcome == "cancelled":
                    assert isinstance(result, asyncio.CancelledError)
                else:
                    assert result.status_code == (200 if outcome == "success" else 503)
                assert (await client.get("/api/tasks/runs")).json()[0]["status"] == outcome
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)
                competitor.release()
                await scheduler.stop()
        assert scheduler.is_quiescent
        orphan = hub.cache.start_task_run("previous-process-orphan")
        assert await scheduler.start()
        await scheduler.stop()
        rows = hub.cache.recent_task_runs()
        assert next(row for row in rows if row.id == orphan).status == "cancelled"
        assert next(row for row in rows if row.task_name == name).status == outcome
    asyncio.run(scenario())


class _ActivationScanner:
    def __init__(self, before_start=None):
        self.before_start = before_start
        self.running = False
        self.starts = 0
        self.rollbacks = 0

    @property
    def is_quiescent(self):
        return not self.running

    async def start(self):
        self.starts += 1
        self.running = True
        if self.before_start is not None:
            await self.before_start()
        return True

    async def rollback_activation(self):
        self.rollbacks += 1
        self.running = False

    async def stop(self):
        self.running = False


async def _until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("retry", [False, True])
def test_coordinator_deferred_start_retains_live_lease_then_retries_activation(tmp_path, retry):
    async def scenario():
        hub = _runtime_hub(tmp_path / "runtime.sqlite3")
        leadership = RuntimeLeadership.for_cache_path(hub.cache.path)
        scheduler = LocalDataScheduler(hub, instance_guard=leadership.service_guard())
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler():
            entered.set()
            await release.wait()
            return "manual success"
        name = _manual_task(scheduler, handler)
        manual = None
        async def start_manual_once():
            nonlocal manual
            if manual is None:
                manual = asyncio.create_task(scheduler.run_once(name))
                await asyncio.wait_for(entered.wait(), 1)
        scanner = _ActivationScanner(start_manual_once)
        coordinator = RuntimeCoordinator(leadership, scheduler, scanner, takeover_poll_seconds=0.05)
        competitor = RuntimeLeadership.for_cache_path(hub.cache.path)
        try:
            assert await coordinator.start() is False
            assert await coordinator.start() is False
            assert not coordinator._active and not scheduler.status().running  # noqa: SLF001
            assert leadership.is_leader and not competitor.try_acquire()
            assert scanner.rollbacks == 1 and not scanner.running
            assert hub.cache.recent_task_runs()[0].status == "running"
            if not retry:
                await coordinator.stop()
                assert leadership.is_leader and not competitor.try_acquire()
            release.set()
            await manual
            if retry:
                await _until(lambda: coordinator._active and scheduler.status().running)  # noqa: SLF001
                assert coordinator._active and scanner.starts == 2  # noqa: SLF001
            else:
                await _until(lambda: not leadership.is_leader)
                assert not scheduler.status().running and scanner.starts == 1
            assert hub.cache.recent_task_runs()[0].status == "success"
        finally:
            release.set()
            if manual is not None:
                await asyncio.gather(manual, return_exceptions=True)
            await coordinator.stop()
            competitor.release()
        assert not leadership.needs_release and scheduler.is_quiescent
    asyncio.run(scenario())


def test_quiescent_start_refusal_releases_lease_and_standby_retries(tmp_path):
    class RefuseOnceScheduler(LocalDataScheduler):
        refused = False

        async def start(self):
            if not self.refused:
                self.refused = True
                return False
            return await super().start()

    async def scenario():
        hub = _runtime_hub(tmp_path / "runtime.sqlite3")
        leadership = RuntimeLeadership.for_cache_path(hub.cache.path)
        scheduler = RefuseOnceScheduler(hub, instance_guard=leadership.service_guard())
        scheduler.tasks = {}
        scanner = _ActivationScanner()
        coordinator = RuntimeCoordinator(leadership, scheduler, scanner, takeover_poll_seconds=0.05)
        competitor = RuntimeLeadership.for_cache_path(hub.cache.path)
        try:
            assert await coordinator.start() is False
            assert not leadership.needs_release and not coordinator._active  # noqa: SLF001
            assert scanner.rollbacks == 1 and not scanner.running
            assert competitor.try_acquire()
            competitor.release()
            await _until(lambda: coordinator._active and scheduler.status().running)  # noqa: SLF001
            assert coordinator._active and scanner.starts == 2  # noqa: SLF001
        finally:
            competitor.release()
            await coordinator.stop()
        assert not leadership.needs_release
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["disabled", "already_running"])
def test_coordinator_accepts_normal_false_scheduler_start_modes(tmp_path, mode):
    async def scenario():
        hub = _runtime_hub(tmp_path / "runtime.sqlite3")
        hub.settings.scheduler_enabled = mode != "disabled"
        leadership = RuntimeLeadership.for_cache_path(hub.cache.path)
        scheduler = LocalDataScheduler(hub, instance_guard=leadership.service_guard())
        scheduler.tasks = {}
        async def already_started():
            if mode == "already_running":
                assert await scheduler.start()
        scanner = _ActivationScanner(already_started)
        coordinator = RuntimeCoordinator(leadership, scheduler, scanner, takeover_poll_seconds=0.05)
        try:
            assert await coordinator.start()
            assert coordinator._active and leadership.is_leader  # noqa: SLF001
            assert scheduler.status().running is (mode == "already_running")
            assert scanner.rollbacks == 0
        finally:
            await coordinator.stop()
        assert not leadership.needs_release
    asyncio.run(scenario())
