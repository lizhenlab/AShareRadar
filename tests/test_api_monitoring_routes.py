from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_datahub, get_scheduler
from app.api.routes import monitoring


def test_task_status_route_maps_runtime_errors_to_api_detail() -> None:
    client = _client(scheduler=_FailingScheduler(RuntimeError("调度器状态不可用")))

    response = client.get("/api/tasks/status")

    assert response.status_code == 503
    assert response.json() == {"detail": "调度器状态不可用"}


def test_task_runs_route_maps_sqlite_errors_to_api_detail() -> None:
    cache = _FailingCache(sqlite3.OperationalError("database is locked"))
    client = _client(datahub=_DataHubStub(cache=cache))

    response = client.get("/api/tasks/runs")

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}


def test_monitor_events_route_maps_sqlite_errors_to_api_detail() -> None:
    cache = _FailingCache(sqlite3.OperationalError("database is locked"))
    client = _client(datahub=_DataHubStub(cache=cache))

    response = client.get("/api/monitor/events")

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}


def test_system_diagnostics_route_maps_sqlite_errors_to_api_detail() -> None:
    cache = _FailingCache(sqlite3.OperationalError("database is locked"))
    client = _client(datahub=_DataHubStub(cache=cache), scheduler=_SchedulerStub())

    response = client.get("/api/system/diagnostics")

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}


def test_run_task_once_route_maps_manual_task_failure_to_api_detail() -> None:
    client = _client(scheduler=_FailingRunOnceScheduler(RuntimeError("刷新失败")))

    response = client.post("/api/tasks/run-once?task=refresh_watch_quotes")

    assert response.status_code == 503
    assert response.json() == {"detail": "刷新失败"}


def test_run_task_once_route_returns_contract() -> None:
    scheduler = _RunOnceScheduler(messages=["刷新行情完成", "刷新K线完成"])
    client = _client(scheduler=scheduler)

    response = client.post("/api/tasks/run-once?task=refresh_watch_quotes")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "messages": ["刷新行情完成", "刷新K线完成"]}
    assert scheduler.task_names == ["refresh_watch_quotes"]


def test_run_task_once_route_uses_run_once_response_model() -> None:
    app = FastAPI()
    app.include_router(monitoring.router)

    schema = app.openapi()["paths"]["/api/tasks/run-once"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    assert schema == {"$ref": "#/components/schemas/TaskRunOnceResponse"}


def _client(datahub=None, scheduler=None) -> TestClient:
    app = FastAPI()
    app.include_router(monitoring.router)
    app.dependency_overrides[get_datahub] = lambda: datahub or _DataHubStub(cache=_EmptyCache())
    app.dependency_overrides[get_scheduler] = lambda: scheduler or _SchedulerStub()
    return TestClient(app)


class _DataHubStub:
    def __init__(self, *, cache) -> None:
        self.cache = cache


class _FailingCache:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def recent_task_runs(self, limit: int = 20):
        raise self._exc

    def recent_monitor_events(self, limit: int = 30):
        raise self._exc

    def stats(self):
        raise self._exc


class _EmptyCache:
    def recent_task_runs(self, limit: int = 20) -> list[object]:
        return []

    def recent_monitor_events(self, limit: int = 30) -> list[object]:
        return []


class _SchedulerStub:
    def status(self):
        return {
            "enabled": True,
            "running": False,
            "started_at": None,
            "task_count": 0,
            "tasks": [],
        }


class _FailingScheduler:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def status(self):
        raise self._exc


class _FailingRunOnceScheduler:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def run_once(self, task_name: str | None = None):
        raise self._exc


class _RunOnceScheduler:
    def __init__(self, *, messages: list[str]) -> None:
        self._messages = messages
        self.task_names: list[str | None] = []

    def status(self):
        return _SchedulerStub().status()

    async def run_once(self, task_name: str | None = None):
        self.task_names.append(task_name)
        return list(self._messages)
