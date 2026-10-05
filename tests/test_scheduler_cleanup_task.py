from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import scheduler_schedule
from app.services.instance_guard import FileInstanceGuard
from app.services.scheduler_contracts import LocalTask, RUNTIME_CLEANUP_TASK_NAME
from app.services.scheduler_schedule import _next_automatic_run_at
from app.services.scheduler_service import LocalDataScheduler
from tests.test_scheduler_modules import _SchedulerHub, _ThreadRecordingCache


@pytest.mark.parametrize("current, expected", [
    ("2026-09-23T08:59:59", "2026-09-23T08:59:59"),
    ("2026-09-23T09:00:00", "2026-09-23T20:00:00"),
    ("2026-09-23T19:59:59", "2026-09-23T20:00:00"),
    ("2026-09-23T20:00:00", "2026-09-23T20:00:00"),
    ("2026-09-24T00:00:00", "2026-09-24T00:00:00"),
    ("2026-09-26T11:00:00", "2026-09-26T20:00:00"),
    ("2099-01-05T23:00:00", "2099-01-05T23:00:00"),
])
def test_retention_uses_quiet_local_window_without_calendar_requests(monkeypatch, current, expected):
    monkeypatch.setattr(scheduler_schedule, "trading_dates_between", lambda *_a, **_kw: pytest.fail("maintenance needs no calendar"))
    task = LocalTask("cleanup", "清理", 3600, lambda: None, datetime.fromisoformat(current), automatic_window="maintenance")
    task.schedule_warning = "previous warning"
    assert _next_automatic_run_at(task, datetime.fromisoformat(current)) == datetime.fromisoformat(expected)
    assert task.schedule_warning is None


@pytest.mark.parametrize("interval, delay", [(60, 300), (3600, 3600), (7200, 7200)])
def test_retention_first_run_waits_configured_interval_and_startup_grace(tmp_path, monkeypatch, interval, delay):
    now = datetime(2026, 9, 23, 21)
    monkeypatch.setattr("app.services.scheduler_service.market_now_naive", lambda: now)
    settings = Settings(cache_path=tmp_path / "unused.sqlite3", runtime_maintenance_interval_seconds=interval)
    scheduler = LocalDataScheduler(SimpleNamespace(settings=settings, cache=_ThreadRecordingCache()))
    task = scheduler.tasks[RUNTIME_CLEANUP_TASK_NAME]
    assert task.interval_seconds == interval
    assert task.next_run_at == now + timedelta(seconds=delay)


@pytest.mark.parametrize("busy_kind", ["scheduler", "scan", "probability", "fuyao"])
def test_retention_waits_for_existing_work_and_recovers_without_failure_backoff(monkeypatch, busy_kind):
    now = datetime(2026, 9, 23, 21)
    monkeypatch.setattr("app.services.scheduler_execution.market_now_naive", lambda: now)
    cache = _ThreadRecordingCache()
    hub = _SchedulerHub(cache=cache)
    scheduler = LocalDataScheduler(hub)
    task = scheduler.tasks[RUNTIME_CLEANUP_TASK_NAME]
    calls = []
    cache.cleanup_regenerable_runtime_rows = lambda: calls.append(True) or {"cache_event": 2}
    other = scheduler.tasks["refresh_key_klines"]
    other.running = busy_kind == "scheduler"
    cache.active_market_scan_run = lambda: object() if busy_kind == "scan" else None
    scheduler.market_scanner = SimpleNamespace(probability_research_refresh_pending=busy_kind == "probability")
    hub.fuyao = SimpleNamespace(has_active_jobs=busy_kind == "fuyao")

    async def run():
        message = await scheduler.run_once(task.name)
        assert "等待空闲" in message[0]
        assert task.last_status == "pending" and task.consecutive_failures == 0
        assert task.next_run_at > now and calls == []
        other.running = False
        cache.active_market_scan_run = lambda: None
        scheduler.market_scanner.probability_research_refresh_pending = False
        hub.fuyao.has_active_jobs = False
        assert await scheduler.run_once(task.name) == ["已清理 2 条过期运行记录"]
        assert task.last_status == "success" and calls == [True]

    asyncio.run(run())


@pytest.mark.parametrize("removed, expected", [({}, "已跳过"), ({"cache_event": 0}, "无需清理")])
def test_manual_retention_is_available_in_daytime_and_reports_no_work(monkeypatch, removed, expected):
    monkeypatch.setattr("app.services.scheduler_execution.market_now_naive", lambda: datetime(2026, 9, 23, 10))
    cache = _ThreadRecordingCache()
    cache.cleanup_regenerable_runtime_rows = lambda: removed
    scheduler = LocalDataScheduler(_SchedulerHub(cache=cache))
    result = asyncio.run(scheduler.run_once(RUNTIME_CLEANUP_TASK_NAME))
    assert expected in result[0]
    assert scheduler.tasks[RUNTIME_CLEANUP_TASK_NAME].last_status == "success"
    assert cache.finished_runs[-1][0] == "success"


def test_retention_failure_remains_observable_and_next_success_resets_backoff(monkeypatch):
    now = datetime(2026, 9, 23, 21)
    monkeypatch.setattr("app.services.scheduler_execution.market_now_naive", lambda: now)
    cache = _ThreadRecordingCache()
    scheduler = LocalDataScheduler(_SchedulerHub(cache=cache))
    task = scheduler.tasks[RUNTIME_CLEANUP_TASK_NAME]
    cache.cleanup_regenerable_runtime_rows = lambda: (_ for _ in ()).throw(RuntimeError("archive changed"))
    with pytest.raises(RuntimeError, match="archive changed"):
        asyncio.run(scheduler.run_once(task.name))
    assert task.last_status == "failed" and task.consecutive_failures == 1
    assert task.next_run_at == now + timedelta(seconds=120)
    cache.cleanup_regenerable_runtime_rows = lambda: {}
    asyncio.run(scheduler.run_once(task.name))
    assert task.last_status == "success" and task.consecutive_failures == 0


def test_retention_cancellation_keeps_instance_guard_until_sync_worker_finishes(tmp_path: Path):
    async def run():
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        cache = _ThreadRecordingCache()

        def cleanup():
            entered.set()
            assert release.wait(5)
            finished.set()
            return {"cache_event": 1}

        cache.cleanup_regenerable_runtime_rows = cleanup
        scheduler = LocalDataScheduler(_SchedulerHub(cache=cache), instance_guard=FileInstanceGuard(tmp_path / "scheduler.lock"))
        competitor = FileInstanceGuard(tmp_path / "scheduler.lock")
        work = asyncio.create_task(scheduler.run_once(RUNTIME_CLEANUP_TASK_NAME))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            work.cancel()
            await asyncio.sleep(0.02)
            work.cancel()
            assert not work.done() and not scheduler.is_quiescent
            assert not competitor.acquire()
            assert cache.finished_runs == []
        finally:
            release.set()
            await asyncio.gather(work, return_exceptions=True)
            competitor.release()
        assert finished.is_set() and work.cancelled() and scheduler.is_quiescent
        assert cache.finished_runs[-1][0] == "cancelled"

    asyncio.run(run())


def test_automatic_retention_stop_is_bounded_and_keeps_guard_until_cleanup_finishes(tmp_path, monkeypatch):
    now = datetime(2026, 9, 23, 21)
    monkeypatch.setattr("app.services.scheduler_execution.market_now_naive", lambda: now)

    async def run():
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        cache = _ThreadRecordingCache()

        def cleanup():
            entered.set()
            assert release.wait(5)
            finished.set()
            return {"cache_event": 1}

        cache.cleanup_regenerable_runtime_rows = cleanup
        settings = Settings(cache_path=tmp_path / "unused.sqlite3", scheduler_enabled=True, scheduler_shutdown_timeout_seconds=0.02)
        scheduler = LocalDataScheduler(SimpleNamespace(settings=settings, cache=cache), instance_guard=FileInstanceGuard(tmp_path / "scheduler.lock"))
        task = scheduler.tasks[RUNTIME_CLEANUP_TASK_NAME]
        task.next_run_at = now
        scheduler.tasks = {task.name: task}
        competitor = FileInstanceGuard(tmp_path / "scheduler.lock")
        try:
            assert await scheduler.start()
            assert await asyncio.to_thread(entered.wait, 1)
            assert await asyncio.wait_for(scheduler.stop(), timeout=0.5)
            assert not finished.is_set() and not scheduler.is_quiescent
            assert not competitor.acquire()
            assert not await scheduler.start()
        finally:
            release.set()
            await asyncio.wait_for(scheduler.wait_until_quiescent(), timeout=1)
            competitor.release()
        assert finished.is_set() and task.last_status == "cancelled"
        assert len(cache.finished_runs) == 1 and cache.finished_runs[0][0] == "cancelled"
        assert competitor.acquire()
        competitor.release()

    asyncio.run(run())
