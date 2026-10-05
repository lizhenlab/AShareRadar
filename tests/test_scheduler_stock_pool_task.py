from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.services import trading_calendar
from app.services.scheduler_contracts import LocalTask
from app.services.scheduler_schedule import _build_local_tasks, _next_automatic_run_at
from app.services.scheduler_service import LocalDataScheduler
from app.services.stock_pool_maintenance import StockPoolMaintenanceResult
from tests.test_scheduler_modules import _SchedulerHub, _handlers, _scheduler_settings


@pytest.fixture(autouse=True)
def isolated_calendar(monkeypatch, tmp_path):
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", tmp_path / "absent-calendar.json")
    trading_calendar._reset_calendar_caches()
    yield
    trading_calendar._reset_calendar_caches()


@pytest.mark.parametrize("current, expected", [
    ("2026-09-23T08:00:00", "2026-09-23T08:30:00"),
    ("2026-09-23T08:30:00", "2026-09-23T08:30:00"),
    ("2026-09-23T09:14:59", "2026-09-23T09:14:59"),
    ("2026-09-23T09:15:00", "2026-09-23T15:15:00"),
    ("2026-09-23T15:14:59", "2026-09-23T15:15:00"),
    ("2026-09-23T15:15:00", "2026-09-23T15:15:00"),
    ("2026-09-23T19:59:59", "2026-09-23T19:59:59"),
    ("2026-09-23T20:00:00", "2026-09-24T08:30:00"),
    ("2026-09-24T20:00:00", "2026-09-28T08:30:00"),
    ("2026-09-30T20:00:00", "2026-10-08T08:30:00"),
])
def test_stock_metadata_refresh_avoids_active_sessions_and_holidays(current, expected):
    now = datetime.fromisoformat(current)
    task = LocalTask("metadata", "股票池", 3600, AsyncMock(), now, automatic_window="stock_metadata")
    assert _next_automatic_run_at(task, now) == datetime.fromisoformat(expected)


def test_stock_metadata_check_frequency_is_independent_of_quote_interval_and_pool_ttl():
    settings = _scheduler_settings()
    settings.scheduler_quote_interval_seconds = 10
    settings.stock_pool_cache_seconds = 86400 * 7
    now = datetime(2026, 9, 23, 8, 45)
    task = _build_local_tasks(settings, now, _handlers())["refresh_stock_pool_metadata"]
    assert task.interval_seconds == 3600
    assert task.next_run_at == now + timedelta(seconds=60)


@pytest.mark.parametrize("status, level", [("success", "info"), ("degraded", "warning")])
def test_stock_metadata_task_preserves_helper_result_and_records_observable_state(monkeypatch, status, level):
    scheduler = LocalDataScheduler(_SchedulerHub())
    refresh = AsyncMock(return_value=StockPoolMaintenanceResult(status, "stock metadata detail"))
    monkeypatch.setattr("app.services.stock_pool_maintenance.refresh_stock_pool_metadata", refresh)
    result = asyncio.run(scheduler.run_once("refresh_stock_pool_metadata"))
    refresh.assert_awaited_once_with(scheduler.datahub)
    assert result == ["stock metadata detail"]
    task = scheduler.tasks["refresh_stock_pool_metadata"]
    assert task.last_status == status
    assert task.consecutive_failures == int(status == "degraded")
    assert scheduler.datahub.cache.monitor_events[-1] == (level, "stock_pool", "stock metadata detail")


def test_stock_metadata_failure_is_persisted_and_next_success_recovers(monkeypatch):
    scheduler = LocalDataScheduler(_SchedulerHub())
    refresh = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr("app.services.stock_pool_maintenance.refresh_stock_pool_metadata", refresh)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        asyncio.run(scheduler.run_once("refresh_stock_pool_metadata"))
    task = scheduler.tasks["refresh_stock_pool_metadata"]
    assert task.last_status == "failed" and task.consecutive_failures == 1
    refresh.side_effect = None
    refresh.return_value = StockPoolMaintenanceResult("success", "fresh cache")
    asyncio.run(scheduler.run_once(task.name))
    assert task.last_status == "success" and task.consecutive_failures == 0
