from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import scheduler_execution, scheduler_schedule, scheduler_service, trading_calendar
from app.services.scheduler_contracts import LocalTask, TaskExecutionResult, TaskWindow
from app.services.scheduler_schedule import (
    _automatic_interval,
    _build_local_tasks,
    _next_automatic_run_at,
    _reschedule_task,
    _task_state,
    automatic_task_due,
)
from app.services.scheduler_service import LocalDataScheduler
from tests.test_scheduler_modules import _SchedulerHub, _handlers, _scheduler_settings


@pytest.fixture(autouse=True)
def isolated_trusted_calendar(monkeypatch, tmp_path):
    # Tests use the checked-in exchange calendar, never local runtime state.
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", tmp_path / "absent-calendar.json")
    trading_calendar._reset_calendar_caches()  # noqa: SLF001
    yield
    trading_calendar._reset_calendar_caches()  # noqa: SLF001


async def _ok() -> str:
    return "ok"


def _task(window: TaskWindow, current: datetime, *, interval: int = 30) -> LocalTask:
    return LocalTask("test", "测试任务", interval, _ok, current, automatic_window=window)


@pytest.mark.parametrize("current,expected", [
    ("2026-09-22T08:00:00", "2026-09-22T09:15:00"),
    ("2026-09-22T09:15:00", "2026-09-22T09:15:00"),
    ("2026-09-22T11:29:59", "2026-09-22T11:29:59"),
    ("2026-09-22T11:30:00", "2026-09-22T13:00:00"),
    ("2026-09-22T12:59:59", "2026-09-22T13:00:00"),
    ("2026-09-22T13:00:00", "2026-09-22T13:00:00"),
    ("2026-09-22T15:00:00", "2026-09-22T15:00:00"),
    ("2026-09-22T15:14:59", "2026-09-22T15:14:59"),
    ("2026-09-22T15:15:00", "2026-09-23T09:15:00"),
    ("2026-09-22T22:00:00", "2026-09-23T09:15:00"),
    ("2026-09-19T10:00:00", "2026-09-21T09:15:00"),
    ("2026-09-24T16:00:00", "2026-09-28T09:15:00"),
    ("2026-09-30T15:15:00", "2026-10-08T09:15:00"),
    ("2026-10-01T09:30:00", "2026-10-08T09:15:00"),
])
def test_market_windows_follow_trusted_sessions_lunch_and_holidays(current: str, expected: str) -> None:
    moment = datetime.fromisoformat(current)
    task = _task("market", moment)
    assert _next_automatic_run_at(task, moment) == datetime.fromisoformat(expected)
    assert task.schedule_warning is None


@pytest.mark.parametrize("window,current,expected", [
    ("daytime", "2026-09-22T09:15:00", "2026-09-22T09:15:00"),
    ("daytime", "2026-09-22T11:30:00", "2026-09-22T13:00:00"),
    ("daytime", "2026-09-22T15:15:00", "2026-09-22T15:15:00"),
    ("daytime", "2026-09-22T19:59:59", "2026-09-22T19:59:59"),
    ("daytime", "2026-09-22T20:00:00", "2026-09-23T09:15:00"),
    ("post_close", "2026-09-22T10:00:00", "2026-09-22T15:15:00"),
    ("post_close", "2026-09-22T15:14:59", "2026-09-22T15:15:00"),
    ("post_close", "2026-09-22T15:15:00", "2026-09-22T15:15:00"),
    ("post_close", "2026-09-22T19:59:59", "2026-09-22T19:59:59"),
    ("post_close", "2026-09-22T20:00:00", "2026-09-23T15:15:00"),
    ("post_close", "2026-09-30T20:00:00", "2026-10-08T15:15:00"),
    ("post_close", "2026-09-20T16:00:00", "2026-09-21T15:15:00"),
])
def test_daily_kline_and_research_windows_have_exclusive_end(window: TaskWindow, current: str, expected: str) -> None:
    moment = datetime.fromisoformat(current)
    assert _next_automatic_run_at(_task(window, moment), moment) == datetime.fromisoformat(expected)


def test_utc_input_is_scheduled_in_shanghai_time() -> None:
    utc = datetime(2026, 9, 22, 3, 30, tzinfo=UTC)
    assert _next_automatic_run_at(_task("market", utc), utc) == datetime(2026, 9, 22, 13)


def test_all_registered_tasks_are_assigned_the_intended_windows() -> None:
    now = datetime(2026, 9, 22, 8)
    tasks = _build_local_tasks(_scheduler_settings(), now, _handlers())
    assert {name: task.automatic_window for name, task in tasks.items()} == {
        "refresh_watch_quotes": "market", "refresh_plate_rank": "market", "evaluate_alerts": "market",
        "refresh_key_klines": "daytime", "run_strategy_schedules": "daytime", "check_data_health": "health",
        "refresh_research_queue": "post_close", "evaluate_due_reviews": "post_close",
        "maintain_market_scan_probability": "post_close",
        "cleanup_runtime_cache": "maintenance",
        "refresh_stock_pool_metadata": "stock_metadata",
    }
    assert tasks["refresh_key_klines"].next_run_at == datetime(2026, 9, 22, 9, 15)
    assert tasks["refresh_research_queue"].next_run_at == datetime(2026, 9, 22, 15, 15)
    assert tasks["check_data_health"].interval_seconds == 300


@pytest.mark.parametrize("window", ["market", "daytime", "post_close", "health", "stock_metadata"])
def test_unknown_calendar_is_visible_and_fail_closed_without_refresh(monkeypatch, window: TaskWindow) -> None:
    now = datetime(2099, 1, 5, 10)
    refreshes = []
    monkeypatch.setattr(trading_calendar, "_trigger_auto_refresh", lambda: refreshes.append(True))
    task = _task(window, now)
    task.last_status, task.last_message = "failed", "真实上游失败"
    assert automatic_task_due(task, now) is False
    assert task.next_run_at == now + timedelta(hours=6)
    state = _task_state(task)
    assert state.last_status == "failed"
    assert state.last_message is not None and "真实上游失败" in state.last_message
    assert "可信交易日历覆盖不足" in state.last_message
    assert refreshes == []
    assert automatic_task_due(task, now + timedelta(minutes=1)) is False


def test_calendar_refresh_can_recover_deferred_task(monkeypatch) -> None:
    now = datetime(2026, 9, 22, 10)
    task = _task("market", now)
    original = scheduler_schedule.trading_dates_between
    def uncovered(*args, **kwargs):
        raise trading_calendar.TradingCalendarCoverageError("not covered")
    monkeypatch.setattr(scheduler_schedule, "trading_dates_between", uncovered)
    assert automatic_task_due(task, now) is False
    monkeypatch.setattr(scheduler_schedule, "trading_dates_between", original)
    next_day = datetime(2026, 9, 23, 10)
    assert automatic_task_due(task, next_day) is True
    assert task.schedule_warning is None


def test_calendar_end_does_not_guess_next_weekday() -> None:
    now = datetime(2026, 12, 31, 20)
    task = _task("post_close", now)
    assert _next_automatic_run_at(task, now) == now + timedelta(hours=6)
    assert "覆盖不足" in task.schedule_warning


@pytest.mark.parametrize("status", ["failed", "degraded"])
def test_backoff_grows_caps_and_success_restores_base(status: str) -> None:
    now = datetime(2026, 9, 22, 10)
    task = _task("always", now)
    delays = []
    for _ in range(100):
        task.last_status = status
        _reschedule_task(task, manual=False, finished_at=now)
        delays.append((task.next_run_at - now).total_seconds())
    assert delays[:5] == [60, 120, 240, 480, 960]
    assert max(delays) == 3600 and task.consecutive_failures == 100
    state = _task_state(task)
    assert state.last_status == status and "100 次" in state.last_message
    task.last_status = "success"
    _reschedule_task(task, manual=False, finished_at=now)
    assert task.next_run_at == now + timedelta(seconds=30)
    assert task.consecutive_failures == 0


def test_backoff_does_not_shorten_long_user_intervals() -> None:
    now = datetime(2026, 9, 22, 10)
    task = _task("always", now, interval=7200)
    task.last_status = "failed"
    _reschedule_task(task, manual=False, finished_at=now)
    assert task.next_run_at == now + timedelta(hours=2)


def test_pending_continuation_clears_backoff_without_claiming_success() -> None:
    now = datetime(2026, 9, 22, 16)
    task = _task("post_close", now)
    task.consecutive_failures = 3
    task.last_status = "pending"
    _reschedule_task(task, manual=False, finished_at=now)
    assert task.last_status == "pending"
    assert task.next_run_at == now + timedelta(seconds=30)
    assert task.consecutive_failures == 0


def test_backoff_projects_retry_into_next_valid_session() -> None:
    now = datetime(2026, 9, 22, 11, 29, 30)
    task = _task("market", now)
    task.last_status = "degraded"
    _reschedule_task(task, manual=False, finished_at=now)
    assert task.next_run_at == datetime(2026, 9, 22, 13)
    assert task.last_status == "degraded" and task.consecutive_failures == 1


@pytest.mark.parametrize("now,delay", [(datetime(2026, 9, 22, 10), 300),
                                      (datetime(2026, 9, 22, 12), 900),
                                      (datetime(2026, 9, 22, 20), 900),
                                      (datetime(2026, 9, 20, 10), 900)])
def test_health_checks_are_low_frequency_without_losing_weekend_monitoring(now: datetime, delay: int) -> None:
    task = _task("health", now, interval=45)
    task.last_status = "success"
    _reschedule_task(task, manual=False, finished_at=now)
    assert task.next_run_at == now + timedelta(seconds=delay)


def test_health_unknown_calendar_preserves_warning() -> None:
    now = datetime(2099, 1, 5, 10)
    task = _task("health", now, interval=45)
    assert _automatic_interval(task, now) == 900
    _reschedule_task(task, manual=False, finished_at=now)
    assert task.schedule_warning and task.next_run_at > now + timedelta(hours=6)


def test_cancelled_task_does_not_count_as_failure_or_clear_prior_failure() -> None:
    now = datetime(2026, 9, 22, 10)
    task = _task("always", now)
    task.consecutive_failures = 2
    task.last_status = "cancelled"
    _reschedule_task(task, manual=False, finished_at=now)
    assert task.consecutive_failures == 2 and task.last_status == "cancelled"
    assert task.next_run_at == now + timedelta(seconds=120)


def _frozen_scheduler(monkeypatch, now: datetime) -> tuple[LocalDataScheduler, _SchedulerHub]:
    monkeypatch.setattr(scheduler_execution, "market_now_naive", lambda: now)
    monkeypatch.setattr(scheduler_service, "market_now_naive", lambda: now)
    hub = _SchedulerHub()
    return LocalDataScheduler(hub), hub


@pytest.mark.parametrize("name", ["refresh_watch_quotes", "refresh_key_klines", "refresh_plate_rank",
                                  "refresh_research_queue", "evaluate_due_reviews",
                                  "maintain_market_scan_probability", "run_strategy_schedules", "evaluate_alerts"])
def test_late_automatic_dispatch_does_not_run_or_persist_fake_success(monkeypatch, name: str) -> None:
    now = datetime(2026, 9, 22, 20)
    scheduler, hub = _frozen_scheduler(monkeypatch, now)
    task = scheduler.tasks[name]
    task.next_run_at = now - timedelta(days=1)
    calls = []
    async def handler():
        calls.append(True)
        return "not allowed"
    task.handler = handler
    task.last_status, task.last_message = "failed", "keep this failure"
    result = asyncio.run(scheduler._execute(task))
    assert "未到自动执行窗口" in result
    assert calls == [] and hub.cache.finished_runs == []
    assert task.last_status == "failed" and task.last_message == "keep this failure"
    assert task.last_started_at is None
    assert task.next_run_at.date() == datetime(2026, 9, 23).date()


@pytest.mark.parametrize("now", [datetime(2026, 9, 20, 23), datetime(2099, 1, 5, 10)])
def test_manual_run_bypasses_window_due_time_and_failure_backoff(monkeypatch, now: datetime) -> None:
    scheduler, hub = _frozen_scheduler(monkeypatch, now)
    task = scheduler.tasks["refresh_watch_quotes"]
    task.next_run_at = now + timedelta(days=10)
    task.consecutive_failures = 6
    result = asyncio.run(scheduler.run_once("refresh_watch_quotes"))
    assert "已刷新" in result[0]
    assert len(hub.quote_calls) == 1
    assert hub.cache.finished_runs[0][0] == "success"
    assert task.consecutive_failures == 0


def test_manual_research_and_review_handlers_can_execute_outside_window(monkeypatch) -> None:
    scheduler, hub = _frozen_scheduler(monkeypatch, datetime(2026, 9, 20, 23))
    calls = []
    async def research(*args, **kwargs):
        calls.append("research")
        return SimpleNamespace(selected_count=1, active_count=1, saved_count=1,
                               unchanged_count=0, skipped_count=0, failed_count=0)
    async def review(*args, **kwargs):
        calls.append("review")
        return SimpleNamespace(candidate_count=1, evaluated_count=1, failed_count=0, attempted_count=1)
    monkeypatch.setattr("app.workflows.individual.refresh_active_research_queue", research)
    monkeypatch.setattr("app.services.advice_review.evaluate_due_advice_reviews", review)
    asyncio.run(scheduler.run_once("refresh_research_queue"))
    asyncio.run(scheduler.run_once("evaluate_due_reviews"))
    assert calls == ["research", "review"]
    assert [status for status, _ in hub.cache.finished_runs] == ["success", "success"]


def test_execution_persists_degraded_failure_recovery_without_masking(monkeypatch) -> None:
    now = datetime(2026, 9, 22, 10)
    scheduler, hub = _frozen_scheduler(monkeypatch, now)
    task = scheduler.tasks["refresh_watch_quotes"]
    outcomes = iter([TaskExecutionResult("cached only", "degraded"), RuntimeError("source down"), "recovered"])
    async def handler():
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result
    task.handler = handler
    for expected in ("degraded", "failed", "success"):
        task.next_run_at = now
        asyncio.run(scheduler._execute(task))
        assert task.last_status == expected
    assert hub.cache.finished_runs == [("degraded", "cached only"), ("failed", "source down"), ("success", "recovered")]
    assert task.consecutive_failures == 0


def test_automatic_cancellation_stays_cancelled_and_keeps_backoff(monkeypatch) -> None:
    now = datetime(2026, 9, 22, 10)
    scheduler, hub = _frozen_scheduler(monkeypatch, now)
    task = scheduler.tasks["refresh_watch_quotes"]
    task.next_run_at, task.consecutive_failures = now, 2
    async def scenario():
        entered = asyncio.Event()
        async def handler():
            entered.set()
            await asyncio.Event().wait()
            return "unreachable"
        task.handler = handler
        running = asyncio.create_task(scheduler._execute(task))
        await entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    asyncio.run(scenario())
    assert task.last_status == "cancelled" and task.consecutive_failures == 2
    assert hub.cache.finished_runs[0][0] == "cancelled"
    assert task.running is False and task.next_run_at > now


def test_existing_not_due_or_running_task_remains_unmodified() -> None:
    now = datetime(2026, 9, 22, 10)
    task = _task("market", now + timedelta(minutes=1))
    assert automatic_task_due(task, now) is False
    task.next_run_at = now
    task.running = True
    assert automatic_task_due(task, now) is False
    assert task.next_run_at == now


def test_automatic_loop_does_not_enqueue_network_or_research_jobs_at_night(monkeypatch) -> None:
    now = datetime(2026, 9, 22, 20)
    scheduler, hub = _frozen_scheduler(monkeypatch, now)
    scheduler.tasks.pop("check_data_health")
    scheduler.tasks.pop("cleanup_runtime_cache")
    for task in scheduler.tasks.values():
        task.next_run_at = now - timedelta(days=1)
    monkeypatch.setattr(scheduler, "_schedule_market_scan_tick", scheduler._stop_event.set)
    asyncio.run(scheduler._loop())
    assert not scheduler._active_tasks
    assert hub.quote_calls == hub.kline_calls == hub.plate_rank_calls == []
    assert hub.cache.finished_runs == []
    assert all(task.next_run_at > now for task in scheduler.tasks.values())


@pytest.mark.parametrize("status", ["running", "success", "degraded", "failed"])
def test_scan_status_reads_navigation_header_without_full_snapshot_verification(monkeypatch, status: str) -> None:
    now = datetime(2026, 9, 22, 17)
    scheduler, _ = _frozen_scheduler(monkeypatch, now)
    scheduler.settings.market_scan_auto_enabled = True
    scheduler.settings.market_scan_schedule_hour = 16
    scheduler.settings.market_scan_schedule_minute = 30
    scheduler.enabled = True
    scheduler._runner = SimpleNamespace(done=lambda: False)
    row = SimpleNamespace(status=status, data_date="2026-09-22", trigger="scheduled",
                          started_at="2026-09-22 16:30:00", finished_at="2026-09-22 16:40:00",
                          message="原始批次状态说明")
    calls = []
    def identities(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(items=[row])
    def forbidden():
        pytest.fail("status must not invoke the expensive verified latest_run")
    scheduler.market_scanner = SimpleNamespace(run_identities=identities, latest_run=forbidden)
    state = scheduler.status().tasks[-1]
    assert calls == [{"page": 1, "page_size": 1}]
    assert state.last_status == status and state.last_message == row.message
    assert state.last_started_at == row.started_at and state.last_finished_at == row.finished_at
    assert state.running is (status == "running")
    assert state.automatic_enabled is True and state.next_run_at == "2026-09-23 16:30:00"


def test_empty_navigation_header_does_not_fall_back_to_verified_latest(monkeypatch) -> None:
    now = datetime(2026, 9, 22, 10)
    scheduler, _ = _frozen_scheduler(monkeypatch, now)
    scheduler.settings.market_scan_auto_enabled = False
    def forbidden():
        pytest.fail("empty header must not trigger full verification")
    scheduler.market_scanner = SimpleNamespace(run_identities=lambda **kwargs: SimpleNamespace(items=[]), latest_run=forbidden)
    state = scheduler.status().tasks[-1]
    assert not state.running
    assert state.last_status is state.last_started_at is state.last_finished_at is state.last_message is None
    assert state.next_run_at is None


def test_failed_navigation_header_is_not_retried_through_verified_reader(monkeypatch) -> None:
    scheduler, _ = _frozen_scheduler(monkeypatch, datetime(2026, 9, 22, 10))
    def identities(**kwargs):
        raise RuntimeError("database unavailable")
    def forbidden():
        pytest.fail("failed identity read must propagate, not retry an expensive read")
    scheduler.market_scanner = SimpleNamespace(run_identities=identities, latest_run=forbidden)
    with pytest.raises(RuntimeError, match="database unavailable"):
        scheduler.status()


def test_real_scanner_status_uses_existing_identity_repository(monkeypatch, tmp_path) -> None:
    from tests.market_scan_test_support import _MarketScanHub, _rule_version, _scanner
    hub = _MarketScanHub(tmp_path)
    run = hub.cache.create_market_scan_run(trigger="manual", rule_version=_rule_version(hub),
                                           as_of="2026-07-17 16:30:00", data_date="2026-07-17", scope="test")
    hub.cache.start_market_scan_run(run.id)
    hub.cache.finish_market_scan_run(run.id, "failed", message="测试扫描失败")
    scanner = _scanner(hub)
    expected = scanner.run_identities(page=1, page_size=1).items[0]
    def forbidden(**kwargs):
        pytest.fail("real scanner monitoring must not verify all frozen result rows")
    monkeypatch.setattr(scanner, "latest_run", forbidden)
    scheduler = LocalDataScheduler(hub, market_scanner=scanner)
    state = scheduler.status().tasks[-1]
    assert state.last_status == expected.status
    assert state.last_message == expected.message
    assert state.last_started_at == expected.started_at
    assert state.last_finished_at == expected.finished_at
    asyncio.run(scanner.stop())
