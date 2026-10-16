from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
import sqlite3
import threading
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest

from app.api.deps import get_scheduler
from app.api.routes.monitoring import router
from app.config import Settings
from app.services.cache import SQLiteCache
from app.services.scheduler_contracts import LocalTask, TaskExecutionResult
from app.services.scheduler_helpers import _offload
from app.services.scheduler_service import LocalDataScheduler


def _scheduler(tmp_path, handler):
    settings = Settings(cache_path=tmp_path / "tasks.sqlite3", scheduler_enabled=False, scheduler_shutdown_timeout_seconds=0.02)
    cache = SQLiteCache(settings=settings)
    scheduler = LocalDataScheduler(SimpleNamespace(settings=settings, cache=cache))
    scheduler.tasks = {name: LocalTask(name, name, 60, handler, datetime(2026, 1, 1)) for name in ("refresh_test", "refresh_second")}
    return scheduler


async def _settle(scheduler):
    for _ in range(200):
        if not scheduler._active_tasks:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("manual request ownership did not settle")


def test_http_receipt_returns_before_completion_and_duplicates_share_one_task_run(tmp_path):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def handler():
            calls.append(1)
            started.set()
            await release.wait()
            return "完成"

        scheduler = _scheduler(tmp_path, handler)
        other = _scheduler(tmp_path, handler)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_scheduler] = lambda: scheduler
        key = str(uuid4())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            missing = await client.get(f"/api/tasks/requests/{key}")
            assert missing.status_code == 404
            assert missing.headers["cache-control"] == "no-store"
            invalid = await client.get("/api/tasks/requests/not-a-uuid")
            assert invalid.status_code == 400
            assert invalid.headers["cache-control"] == "no-store"
            first = await client.post(f"/api/tasks/run-once?task=refresh_test&request_id={key}")
            assert first.status_code == 202
            assert first.headers["cache-control"] == "no-store"
            assert first.json()["receipt"]["request_id"] == key
            await asyncio.wait_for(started.wait(), 1)
            running = await client.get(f"/api/tasks/requests/{key}")
            assert running.headers["cache-control"] == "no-store"
            assert running.json()["status"] == "running"
            assert len(running.json()["runs"]) == 1
            duplicate = await client.post(f"/api/tasks/run-once?task=refresh_test&request_id={key}")
            assert duplicate.json()["receipt"]["request_id"] == key
            app.dependency_overrides[get_scheduler] = lambda: other
            assert (await client.post(f"/api/tasks/run-once?task=refresh_test&request_id={key}")).status_code == 202
            assert (await client.get(f"/api/tasks/requests/{key}")).json()["status"] == "running"
            assert calls == [1]
            conflict = await client.post(f"/api/tasks/run-once?task=refresh_second&request_id={key}")
            assert conflict.status_code == 400
            release.set()
            await _settle(scheduler)
            await _settle(other)
            final = (await client.get(f"/api/tasks/requests/{key}")).json()
            assert final["status"] == "completed"
            assert final["messages"] == ["完成"]
            assert final["runs"][0]["status"] == "success"
            assert scheduler.is_quiescent and other.is_quiescent

    asyncio.run(scenario())


def test_http_waiter_cancellation_during_receipt_commit_does_not_cancel_execution(tmp_path, monkeypatch):
    async def scenario():
        reserving, release_commit = threading.Event(), threading.Event()
        executed = asyncio.Event()

        async def handler():
            executed.set()
            return "完成"

        scheduler = _scheduler(tmp_path, handler)
        repository = scheduler.datahub.cache.runtime_event_repo
        reserve = repository.reserve_task_request

        def blocked_reserve(*args):
            reserving.set()
            assert release_commit.wait(2)
            return reserve(*args)

        monkeypatch.setattr(repository, "reserve_task_request", blocked_reserve)
        key = str(uuid4())
        waiter = asyncio.create_task(scheduler.submit_run_once("refresh_test", key))
        assert await asyncio.to_thread(reserving.wait, 1)
        waiter.cancel()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert scheduler._active_tasks
        release_commit.set()
        await asyncio.wait_for(executed.wait(), 2)
        await _settle(scheduler)
        receipt = await scheduler.read_run_once_receipt(key)
        assert receipt.status == "completed"
        assert len(receipt.runs) == 1
        assert scheduler.is_quiescent

    asyncio.run(scenario())


def test_request_worker_cancelled_before_first_schedule_releases_the_waiter(tmp_path, monkeypatch):
    async def scenario():
        async def handler():
            pytest.fail("cancelled worker executed its task")

        scheduler = _scheduler(tmp_path, handler)
        create_task = asyncio.create_task

        def cancelled_create(coroutine, *, name=None, **kwargs):
            task = create_task(coroutine, name=name, **kwargs)
            if name == "scheduler-manual-request":
                task.cancel()
            return task

        monkeypatch.setattr(asyncio, "create_task", cancelled_create)
        with pytest.raises(RuntimeError, match="未确认"):
            await asyncio.wait_for(scheduler.submit_run_once("refresh_test", str(uuid4())), 1)
        await _settle(scheduler)
        assert not scheduler._manual_requests
        assert scheduler.is_quiescent

    asyncio.run(scenario())


def test_admission_lookup_is_owned_before_its_scheduler_lease_is_acquired(tmp_path, monkeypatch):
    async def scenario():
        reading, release = threading.Event(), threading.Event()
        executed = []

        async def handler():
            executed.append(True)
            return "完成"

        scheduler = _scheduler(tmp_path, handler)
        repo = scheduler.datahub.cache.runtime_event_repo
        find = repo.find_task_request

        def slow_find(*args):
            reading.set()
            assert release.wait(2)
            return find(*args)

        monkeypatch.setattr(repo, "find_task_request", slow_find)
        waiter = asyncio.create_task(scheduler.submit_run_once("refresh_test", str(uuid4())))
        assert await asyncio.to_thread(reading.wait, 1)
        quiescent = asyncio.create_task(scheduler.wait_until_quiescent())
        await asyncio.sleep(0)
        assert not quiescent.done()
        await scheduler.stop()
        assert not quiescent.done()
        release.set()
        with pytest.raises(RuntimeError, match="未确认"):
            await waiter
        await asyncio.wait_for(quiescent, 1)
        assert scheduler.is_quiescent
        assert executed == []

    asyncio.run(scenario())


def test_shutdown_retains_the_request_and_lease_until_real_worker_exits(tmp_path):
    async def scenario():
        working, release = threading.Event(), threading.Event()

        def slow():
            working.set()
            assert release.wait(2)

        async def handler():
            await _offload(slow)
            return "完成"

        scheduler = _scheduler(tmp_path, handler)
        key = str(uuid4())
        await scheduler.submit_run_once("refresh_test", key)
        assert await asyncio.to_thread(working.wait, 1)
        await scheduler.stop()
        assert not scheduler.is_quiescent
        assert scheduler._guard_acquired
        assert scheduler.datahub.cache.runtime_event_repo.task_request(key).status == "running"
        release.set()
        await asyncio.wait_for(scheduler.wait_until_quiescent(), 2)
        receipt = await scheduler.read_run_once_receipt(key)
        assert receipt.status == "interrupted"
        assert receipt.runs[0].status == "cancelled"

    asyncio.run(scenario())


def test_finished_request_stays_idempotent_when_task_history_is_deleted(tmp_path):
    async def scenario():
        calls = []

        async def handler():
            calls.append(1)
            return "完成"

        scheduler = _scheduler(tmp_path, handler)
        key = str(uuid4())
        await scheduler.submit_run_once("refresh_test", key)
        await _settle(scheduler)
        with scheduler.datahub.cache._connect() as conn:
            conn.execute("DELETE FROM task_run")
        duplicate = await scheduler.submit_run_once("refresh_test", key)
        await _settle(scheduler)
        assert duplicate.status == "completed"
        assert duplicate.runs == []
        assert calls == [1]

    asyncio.run(scenario())


def test_restart_reports_interruption_and_never_reexecutes_the_old_request(tmp_path):
    async def scenario():
        calls = []

        async def handler():
            calls.append(1)
            return "完成"

        scheduler = _scheduler(tmp_path, handler)
        repo = scheduler.datahub.cache.runtime_event_repo
        key = str(uuid4())
        repo.reserve_task_request(key, "refresh_test")
        run_id = repo.start_task_run("refresh_test")
        repo.attach_task_request_run(key, run_id)
        assert (await scheduler.read_run_once_receipt(key)).status == "interrupted"
        await scheduler.submit_run_once("refresh_test", key)
        await _settle(scheduler)
        assert calls == []
        await scheduler.submit_run_once("refresh_test", str(uuid4()))
        await _settle(scheduler)
        assert repo.task_request(key).status == "interrupted"
        assert repo.task_request(key).runs[0].status == "cancelled"
        assert calls == [1]

    asyncio.run(scenario())


def test_request_reservation_rolls_back_when_model_or_commit_fails(tmp_path, monkeypatch):
    scheduler = _scheduler(tmp_path, lambda: None)
    repo = scheduler.datahub.cache.runtime_event_repo
    connect = repo._connect

    @contextmanager
    def failing_commit():
        with connect() as conn:
            yield conn
            raise sqlite3.OperationalError("commit failed")

    key = str(uuid4())
    monkeypatch.setattr(repo, "_connect", failing_commit)
    with pytest.raises(sqlite3.OperationalError):
        repo.reserve_task_request(key, "refresh_test")
    monkeypatch.setattr(repo, "_connect", connect)
    assert repo.find_task_request(key, "refresh_test") is None


@pytest.mark.parametrize("outcome", ["success", "degraded", "pending", "failed", "skipped"])
def test_receipt_preserves_actual_execution_outcome(tmp_path, outcome):
    async def scenario():
        async def handler():
            if outcome == "failed":
                raise ValueError("刷新失败")
            return TaskExecutionResult("刷新结果", outcome)

        scheduler = _scheduler(tmp_path, handler)
        if outcome == "skipped":
            scheduler.tasks["refresh_test"].running = True
        key = str(uuid4())
        await scheduler.submit_run_once("refresh_test", key)
        await _settle(scheduler)
        receipt = await scheduler.read_run_once_receipt(key)
        assert receipt.status == ("failed" if outcome == "failed" else "completed")
        if outcome == "skipped":
            assert receipt.runs == []
            assert "跳过重复触发" in receipt.messages[0]
        else:
            assert [run.status for run in receipt.runs] == [outcome]

    asyncio.run(scenario())


def test_completion_during_receipt_ownership_probe_is_not_reported_as_interrupted(tmp_path, monkeypatch):
    async def scenario():
        scheduler = _scheduler(tmp_path, lambda: None)
        repo = scheduler.datahub.cache.runtime_event_repo
        key = str(uuid4())
        repo.reserve_task_request(key, "refresh_test")

        def owner_finishes():
            repo.finish_task_request(key, "completed", ["完成"])
            return False

        monkeypatch.setattr(scheduler._instance_guard, "held_by_other", owner_finishes)
        assert (await scheduler.read_run_once_receipt(key)).status == "completed"

    asyncio.run(scenario())


def test_real_sqlite_commit_failure_rolls_back_request_and_releases_scheduler_lease(tmp_path):
    async def scenario():
        scheduler = _scheduler(tmp_path, lambda: None)
        repo = scheduler.datahub.cache.runtime_event_repo
        with repo._connect() as conn:
            conn.executescript("""
                CREATE TABLE commit_parent(id INTEGER PRIMARY KEY);
                CREATE TABLE commit_child(id INTEGER REFERENCES commit_parent(id) DEFERRABLE INITIALLY DEFERRED);
                CREATE TRIGGER fail_request_commit AFTER INSERT ON task_run_request BEGIN
                    INSERT INTO commit_child(id) VALUES(1);
                END;
            """)
        key = str(uuid4())
        with pytest.raises(RuntimeError, match="未确认"):
            await scheduler.submit_run_once("refresh_test", key)
        await _settle(scheduler)
        assert repo.find_task_request(key, "refresh_test") is None
        assert scheduler.is_quiescent

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["", "../private", "not-a-uuid", "A" * 36])
def test_invalid_request_id_never_admits_execution(tmp_path, key):
    async def scenario():
        scheduler = _scheduler(tmp_path, lambda: None)
        with pytest.raises(ValueError, match="编号无效"):
            await scheduler.submit_run_once("refresh_test", key)
        assert not scheduler._active_tasks
        assert scheduler.is_quiescent

    asyncio.run(scenario())
