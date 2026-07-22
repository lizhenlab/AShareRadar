from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import gc
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.schemas import CacheStats, ProviderCapabilityStatus, ProviderStatus
from app.services.instance_guard import FileInstanceGuard
from app.services.scheduler import (
    FileSchedulerInstanceGuard,
    LocalDataScheduler,
    LocalTask,
)
from app.services.scheduler_execution import _next_market_scan_run_at
from app.services.scheduler_health import _data_health_events, _runtime_cleanup_message
from app.services.scheduler_schedule import _build_local_tasks, _reschedule_task, _task_state
from app.utils.time import seconds_ago_text


async def _handler() -> str:
    return "ok"


def test_scheduler_uses_datahub_settings_instance() -> None:
    settings = SimpleNamespace(
        scheduler_enabled=False,
        scheduler_quote_interval_seconds=11,
        scheduler_kline_interval_seconds=121,
        scheduler_plate_interval_seconds=122,
        scheduler_health_interval_seconds=21,
    )
    scheduler = LocalDataScheduler(SimpleNamespace(settings=settings))  # type: ignore[arg-type]

    assert scheduler.settings is settings


def test_file_scheduler_guard_allows_only_one_holder(tmp_path) -> None:
    path = tmp_path / "scheduler.lock"
    first = FileSchedulerInstanceGuard(path)
    second = FileSchedulerInstanceGuard(path)

    assert first.acquire() is True
    assert second.acquire() is False

    first.release()

    assert second.acquire() is True
    second.release()


def test_file_scheduler_guard_keeps_shared_guard_compatibility() -> None:
    assert issubclass(FileSchedulerInstanceGuard, FileInstanceGuard)


def test_scheduler_single_instance_strategy_can_be_injected() -> None:
    guard = _ExclusiveGuard()
    first_hub = _SchedulerHub()
    second_hub = _SchedulerHub()
    first_hub.settings.scheduler_enabled = True
    second_hub.settings.scheduler_enabled = True
    first = LocalDataScheduler(first_hub, instance_guard=guard)
    second = LocalDataScheduler(second_hub, instance_guard=guard)

    async def run_check() -> tuple[bool, bool, bool]:
        first_started = await first.start()
        duplicate_started = await second.start()
        await first.stop()
        second_started = await second.start()
        await second.stop()
        return first_started, duplicate_started, second_started

    assert asyncio.run(run_check()) == (True, False, True)
    assert guard.acquire_calls == 3
    assert guard.release_calls == 2
    assert second_hub.cache.monitor_events[0] == (
        "info",
        "scheduler",
        "已有其他进程运行本地数据调度器，本进程已跳过启动",
    )


def test_non_lock_holder_reports_standby_instead_of_stopped() -> None:
    guard = _ExclusiveGuard()
    holder_hub = _SchedulerHub()
    standby_hub = _SchedulerHub()
    holder_hub.settings.scheduler_enabled = True
    standby_hub.settings.scheduler_enabled = True
    holder = LocalDataScheduler(holder_hub, instance_guard=guard)
    standby = LocalDataScheduler(standby_hub, instance_guard=guard)

    async def run_check():
        assert await holder.start() is True
        assert await standby.start() is False
        status = standby.status()
        assert await holder.stop() is True
        return status

    status = asyncio.run(run_check())

    assert status.running is False
    assert status.standby is True
    assert status.message == "其他实例持有调度器锁，本进程待命"


def test_file_scheduler_standby_status_clears_after_other_holder_releases(tmp_path) -> None:
    lock_path = tmp_path / "scheduler.lock"
    holder_hub = _SchedulerHub()
    standby_hub = _SchedulerHub()
    holder_hub.settings.scheduler_enabled = True
    standby_hub.settings.scheduler_enabled = True
    holder = LocalDataScheduler(holder_hub, instance_guard=FileSchedulerInstanceGuard(lock_path))
    standby = LocalDataScheduler(standby_hub, instance_guard=FileSchedulerInstanceGuard(lock_path))

    async def run_check():
        assert await holder.start() is True
        assert await standby.start() is False
        while_locked = standby.status()
        assert await holder.stop() is True
        after_release = standby.status()
        return while_locked, after_release

    while_locked, after_release = asyncio.run(run_check())

    assert while_locked.standby is True
    assert after_release.standby is False
    assert after_release.message is None


def test_scheduler_start_stop_are_idempotent_and_release_guard() -> None:
    guard = _ExclusiveGuard()
    hub = _SchedulerHub()
    hub.settings.scheduler_enabled = True
    scheduler = LocalDataScheduler(hub, instance_guard=guard)

    async def run_check() -> tuple[bool, bool, bool, bool]:
        return await scheduler.start(), await scheduler.start(), await scheduler.stop(), await scheduler.stop()

    assert asyncio.run(run_check()) == (True, False, True, False)
    assert guard.release_calls == 1
    assert scheduler._runner is None  # noqa: SLF001


def test_scheduler_reconciles_orphaned_runs_only_after_acquiring_instance_guard() -> None:
    guard = _ExclusiveGuard()
    holder_cache = _SchedulerCache()
    holder_cache.orphaned_runs = 2
    holder_hub = _SchedulerHub(cache=holder_cache)
    holder_hub.settings.scheduler_enabled = True
    holder = LocalDataScheduler(holder_hub, instance_guard=guard)
    blocked_cache = _SchedulerCache()
    blocked_hub = _SchedulerHub(cache=blocked_cache)
    blocked_hub.settings.scheduler_enabled = True
    blocked = LocalDataScheduler(blocked_hub, instance_guard=guard)

    async def run_check() -> None:
        assert await holder.start() is True
        assert await blocked.start() is False
        await holder.stop()

    asyncio.run(run_check())

    assert holder_cache.reconcile_calls == 1
    assert blocked_cache.reconcile_calls == 0
    assert (
        "warning",
        "scheduler",
        "应用启动时已终止 2 条遗留运行记录",
    ) in holder_cache.monitor_events


def test_run_once_respects_guard_held_by_another_scheduler() -> None:
    guard = _ExclusiveGuard()
    holder_hub = _SchedulerHub()
    holder_hub.settings.scheduler_enabled = True
    holder = LocalDataScheduler(holder_hub, instance_guard=guard)
    for task in holder.tasks.values():
        task.next_run_at = datetime.now() + timedelta(days=1)

    calls: list[str] = []
    manual = LocalDataScheduler(_SchedulerHub(), instance_guard=guard)
    manual.tasks = {
        "manual": LocalTask(
            "manual",
            "手动任务",
            20,
            _recording_handler("manual", calls),
            datetime.now(),
        )
    }

    async def run_check() -> None:
        assert await holder.start() is True
        with pytest.raises(RuntimeError, match="手动任务未执行"):
            await manual.run_once("manual")
        await holder.stop()

    asyncio.run(run_check())

    assert calls == []
    assert guard.acquire_calls == 2
    assert guard.release_calls == 1


def test_run_once_reuses_guard_already_held_by_started_scheduler() -> None:
    guard = _ExclusiveGuard()
    hub = _SchedulerHub()
    hub.settings.scheduler_enabled = True
    scheduler = LocalDataScheduler(hub, instance_guard=guard)
    calls: list[str] = []
    scheduler.tasks = {
        "manual": LocalTask(
            "manual",
            "手动任务",
            20,
            _recording_handler("manual", calls),
            datetime.now() + timedelta(days=1),
        )
    }

    async def run_check() -> list[str]:
        assert await scheduler.start() is True
        messages = await scheduler.run_once("manual")
        await scheduler.stop()
        return messages

    assert asyncio.run(run_check()) == ["manual"]
    assert calls == ["manual"]
    assert guard.acquire_calls == 1
    assert guard.release_calls == 1


def test_stop_does_not_wait_for_manual_run_and_releases_guard_afterward() -> None:
    guard = _ExclusiveGuard()
    hub = _SchedulerHub()
    hub.settings.scheduler_enabled = True
    scheduler = LocalDataScheduler(hub, instance_guard=guard)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_handler() -> str:
        started.set()
        await release.wait()
        return "manual-finished"

    scheduler.tasks = {
        "manual": LocalTask(
            "manual",
            "手动任务",
            20,
            blocking_handler,
            datetime.now() + timedelta(days=1),
        )
    }

    async def run_check() -> tuple[float, int, list[str]]:
        assert await scheduler.start() is True
        manual_run = asyncio.create_task(scheduler.run_once("manual"))
        await started.wait()
        loop = asyncio.get_running_loop()
        fallback_release = loop.call_later(0.5, release.set)
        began = loop.time()
        assert await scheduler.stop() is True
        elapsed = loop.time() - began
        releases_at_stop = guard.release_calls
        release.set()
        fallback_release.cancel()
        messages = await manual_run
        return elapsed, releases_at_stop, messages

    elapsed, releases_at_stop, messages = asyncio.run(run_check())

    assert elapsed < 0.25
    assert releases_at_stop == 0
    assert messages == ["manual-finished"]
    assert guard.acquire_calls == 1
    assert guard.release_calls == 1


def test_scheduler_health_maintenance_runs_off_event_loop() -> None:
    cache = _ThreadRecordingCache()
    hub = _SchedulerHub(cache=cache)
    hub.settings.quote_stale_warning_seconds = 60
    hub.settings.kline_cache_seconds = 300
    scheduler = LocalDataScheduler(hub)

    async def run_check() -> tuple[int, str]:
        event_loop_thread = threading.get_ident()
        message = await scheduler._check_data_health()
        return event_loop_thread, message

    event_loop_thread, message = asyncio.run(run_check())

    assert cache.cleanup_thread_id is not None
    assert cache.cleanup_thread_id != event_loop_thread
    assert "尚未形成报价缓存" in message


def test_scheduler_start_cancellation_releases_guard_acquired_in_worker() -> None:
    guard = _BlockingGuard()
    hub = _SchedulerHub()
    hub.settings.scheduler_enabled = True
    scheduler = LocalDataScheduler(hub, instance_guard=guard)

    async def run_check() -> None:
        starting = asyncio.create_task(scheduler.start())
        await asyncio.to_thread(guard.acquire_started.wait)
        starting.cancel()
        guard.allow_acquire.set()
        with pytest.raises(asyncio.CancelledError):
            await starting

    asyncio.run(run_check())

    assert guard.release_calls == 1
    assert guard.acquired is False
    assert scheduler._runner is None  # noqa: SLF001


def test_scheduler_start_cancellation_after_runner_creation_cleans_state() -> None:
    guard = _ExclusiveGuard()
    cache = _BlockingStartEventCache()
    hub = _SchedulerHub(cache=cache)
    hub.settings.scheduler_enabled = True
    scheduler = LocalDataScheduler(hub, instance_guard=guard)
    for task in scheduler.tasks.values():
        task.next_run_at = datetime.now() + timedelta(days=1)

    async def run_check() -> None:
        starting = asyncio.create_task(scheduler.start())
        await asyncio.to_thread(cache.start_event_started.wait)
        starting.cancel()
        cache.allow_start_event.set()
        with pytest.raises(asyncio.CancelledError):
            await starting

    asyncio.run(run_check())

    assert guard.release_calls == 1
    assert guard.acquired is False
    assert scheduler._runner is None  # noqa: SLF001
    assert scheduler.started_at is None


def test_scheduler_stop_keeps_guard_until_stubborn_runner_finishes() -> None:
    guard = _ExclusiveGuard()
    first_hub = _SchedulerHub()
    second_hub = _SchedulerHub()
    first_hub.settings.scheduler_enabled = True
    second_hub.settings.scheduler_enabled = True
    first_hub.settings.scheduler_shutdown_timeout_seconds = 0.01
    first = LocalDataScheduler(first_hub, instance_guard=guard)
    second = LocalDataScheduler(second_hub, instance_guard=guard)
    for scheduler in (first, second):
        for task in scheduler.tasks.values():
            task.next_run_at = datetime.now() + timedelta(days=1)

    async def run_check() -> tuple[bool, bool, bool, float, bool, int, list[dict]]:
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop_errors: list[dict] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def stubborn_runner() -> None:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            finally:
                finished.set()
            raise RuntimeError("late scheduler shutdown failure")

        first._loop = stubborn_runner  # type: ignore[method-assign]  # noqa: SLF001
        assert await first.start() is True
        await started.wait()
        fallback_release = loop.call_later(0.5, release.set)
        began = loop.time()
        stopped = await first.stop()
        elapsed = loop.time() - began
        assert first.is_quiescent is False
        duplicate_started = await second.start()
        releases_while_runner_alive = guard.release_calls
        release.set()
        fallback_release.cancel()
        await finished.wait()

        async def wait_for_guard_release() -> None:
            while guard.acquired:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_guard_release(), timeout=1)
        await asyncio.wait_for(first.wait_until_quiescent(), timeout=1)
        assert first.is_quiescent is True
        restarted = await second.start()
        assert await second.stop() is True
        gc.collect()
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)
        return (
            stopped,
            duplicate_started,
            restarted,
            elapsed,
            cancelled.is_set(),
            releases_while_runner_alive,
            loop_errors,
        )

    stopped, duplicate_started, restarted, elapsed, cancelled, early_releases, loop_errors = asyncio.run(run_check())

    assert stopped is True
    assert duplicate_started is False
    assert restarted is True
    assert elapsed < 0.25
    assert cancelled is True
    assert early_releases == 0
    assert guard.acquire_calls == 3
    assert guard.release_calls == 2
    assert loop_errors == []
    assert first._runner is None  # noqa: SLF001


def test_scheduler_stop_cancellation_propagates_without_releasing_stubborn_runner_guard() -> None:
    guard = _ExclusiveGuard()
    hub = _SchedulerHub()
    hub.settings.scheduler_enabled = True
    hub.settings.scheduler_shutdown_timeout_seconds = 0.01
    scheduler = LocalDataScheduler(hub, instance_guard=guard)
    for task in scheduler.tasks.values():
        task.next_run_at = datetime.now() + timedelta(days=1)

    async def run_check() -> tuple[bool, int]:
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def stubborn_runner() -> None:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

        scheduler._loop = stubborn_runner  # type: ignore[method-assign]  # noqa: SLF001
        assert await scheduler.start() is True
        await started.wait()
        stopping = asyncio.create_task(scheduler.stop())
        await asyncio.sleep(0)
        stopping.cancel()
        fallback_release = asyncio.get_running_loop().call_later(0.5, release.set)
        with pytest.raises(asyncio.CancelledError):
            await stopping
        guard_held_after_cancel = guard.acquired
        releases_after_cancel = guard.release_calls
        assert cancelled.is_set()
        release.set()
        fallback_release.cancel()

        async def wait_for_guard_release() -> None:
            while guard.acquired:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_guard_release(), timeout=1)
        return guard_held_after_cancel, releases_after_cancel

    guard_held_after_cancel, releases_after_cancel = asyncio.run(run_check())

    assert guard_held_after_cancel is True
    assert releases_after_cancel == 0
    assert guard.release_calls == 1


def test_build_local_tasks_uses_explicit_specs_min_intervals_and_offsets() -> None:
    now = datetime(2026, 5, 13, 9, 30, 0)
    tasks = _build_local_tasks(_scheduler_settings(), now, _handlers())

    assert list(tasks) == [
        "refresh_watch_quotes",
        "refresh_key_klines",
        "refresh_plate_rank",
        "check_data_health",
        "evaluate_alerts",
    ]
    assert [task.interval_seconds for task in tasks.values()] == [10, 120, 120, 20, 30]
    assert [(task.next_run_at - now).total_seconds() for task in tasks.values()] == [0, 8, 12, 16, 20]


def test_scheduler_loop_lets_market_scanner_use_its_shanghai_clock() -> None:
    scanner = _ScheduledTickScanner()
    scheduler = LocalDataScheduler(_SchedulerHub(), market_scanner=scanner)  # type: ignore[arg-type]
    for task in scheduler.tasks.values():
        task.next_run_at = datetime.now() + timedelta(days=1)
    scanner.on_tick = scheduler._stop_event.set  # noqa: SLF001

    asyncio.run(scheduler._loop())  # noqa: SLF001

    assert scanner.calls == [None]


@pytest.mark.parametrize(
    ("now", "hour", "minute", "expected"),
    [
        (datetime(2026, 7, 17, 14, 0), 14, 0, datetime(2026, 7, 17, 15, 15)),
        (datetime(2026, 7, 17, 14, 0), 16, 30, datetime(2026, 7, 17, 16, 30)),
        (datetime(2026, 7, 18, 10, 0), 14, 0, datetime(2026, 7, 20, 15, 15)),
        (datetime(2026, 7, 17, 17, 0), 16, 30, datetime(2026, 7, 17, 17, 0)),
    ],
)
def test_next_market_scan_run_respects_publish_floor_configured_time_and_trading_days(
    now: datetime,
    hour: int,
    minute: int,
    expected: datetime,
) -> None:
    settings = SimpleNamespace(
        market_scan_schedule_hour=hour,
        market_scan_schedule_minute=minute,
    )

    assert _next_market_scan_run_at(settings, None, now) == expected


@pytest.mark.parametrize(
    ("status", "trigger", "expected"),
    [
        ("success", "manual", datetime(2026, 7, 20, 16, 30)),
        ("cancelled", "manual", datetime(2026, 7, 20, 16, 30)),
        ("failed", "scheduled", datetime(2026, 7, 20, 16, 30)),
        ("failed", "retry", datetime(2026, 7, 20, 16, 30)),
        ("failed", "manual", datetime(2026, 7, 17, 17, 0)),
        ("interrupted", "manual", datetime(2026, 7, 17, 17, 0)),
    ],
)
def test_next_market_scan_run_matches_same_day_attempt_deduplication(
    status: str,
    trigger: str,
    expected: datetime,
) -> None:
    settings = SimpleNamespace(
        market_scan_schedule_hour=16,
        market_scan_schedule_minute=30,
    )
    latest = SimpleNamespace(
        data_date="2026-07-17",
        status=status,
        trigger=trigger,
    )

    assert _next_market_scan_run_at(settings, latest, datetime(2026, 7, 17, 17, 0)) == expected  # type: ignore[arg-type]


def test_market_scan_task_status_reports_disabled_automation_without_hiding_manual_task() -> None:
    hub = _SchedulerHub()
    hub.settings.market_scan_auto_enabled = False
    hub.settings.market_scan_schedule_hour = 16
    hub.settings.market_scan_schedule_minute = 30
    scheduler = LocalDataScheduler(hub, market_scanner=_StatusMarketScanner())  # type: ignore[arg-type]

    status = scheduler.status()
    scan = status.tasks[-1]

    assert status.enabled is False
    assert scan.name == "full_market_scan"
    assert scan.automatic_enabled is False
    assert scan.next_run_at is None


def test_market_scan_task_status_reports_standby_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = _SchedulerHub()
    hub.settings.scheduler_enabled = True
    hub.settings.market_scan_auto_enabled = True
    hub.settings.market_scan_schedule_hour = 14
    hub.settings.market_scan_schedule_minute = 0
    scheduler = LocalDataScheduler(
        hub,
        instance_guard=_HeldByOtherGuard(),
        market_scanner=_StatusMarketScanner(),  # type: ignore[arg-type]
    )
    scheduler.set_runtime_standby(True)
    monkeypatch.setattr(
        "app.services.scheduler_execution.market_now_naive",
        lambda: datetime(2026, 7, 17, 14, 0),
    )

    status = scheduler.status()
    scan = status.tasks[-1]

    assert status.standby is True
    assert scan.automatic_enabled is True
    assert scan.next_run_at == "2026-07-17 15:15:00"


def test_build_local_tasks_clamps_invalid_interval_settings() -> None:
    now = datetime(2026, 5, 13, 9, 30, 0)
    settings = SimpleNamespace(
        scheduler_quote_interval_seconds=float("inf"),
        scheduler_kline_interval_seconds=" ",
        scheduler_plate_interval_seconds=-1,
        scheduler_health_interval_seconds="45",
    )

    tasks = _build_local_tasks(settings, now, _handlers())

    assert [task.interval_seconds for task in tasks.values()] == [10, 120, 120, 45, 30]


def test_run_once_and_status_use_task_spec_order() -> None:
    calls: list[str] = []
    scheduler = LocalDataScheduler(_SchedulerHub())
    now = datetime(2026, 5, 13, 9, 30, 0)
    scheduler.tasks = {
        "zz_custom": LocalTask("zz_custom", "自定义任务", 20, _recording_handler("zz_custom", calls), now),
        "evaluate_alerts": LocalTask(
            "evaluate_alerts",
            "评估本地预警",
            20,
            _recording_handler("evaluate_alerts", calls),
            now,
        ),
        "refresh_watch_quotes": LocalTask(
            "refresh_watch_quotes",
            "刷新观察池报价",
            20,
            _recording_handler("refresh_watch_quotes", calls),
            now,
        ),
    }

    messages = asyncio.run(scheduler.run_once())

    assert calls == ["refresh_watch_quotes", "evaluate_alerts", "zz_custom"]
    assert messages == ["refresh_watch_quotes", "evaluate_alerts", "zz_custom"]
    assert [task.name for task in scheduler.status().tasks] == [
        "refresh_watch_quotes",
        "evaluate_alerts",
        "zz_custom",
    ]


def test_alert_scheduler_marks_all_rule_failures_as_task_failure() -> None:
    scheduler = LocalDataScheduler(_SchedulerHub())
    summary = SimpleNamespace(
        checked_count=2,
        triggered_count=0,
        new_event_count=0,
        failed_count=2,
    )

    with patch("app.services.alerts.evaluate_alert_rules", return_value=summary):
        with pytest.raises(RuntimeError, match="失败 2 条"):
            asyncio.run(scheduler._evaluate_alerts())

    assert scheduler.datahub.cache.monitor_events[-1] == (
        "warning",
        "alert",
        "已评估 2 条本地预警，当前触发 0 条，新增事件 0 条，失败 2 条",
    )


def test_alert_scheduler_persists_partial_rule_failures_as_degraded() -> None:
    scheduler = LocalDataScheduler(_SchedulerHub())
    summary = SimpleNamespace(
        checked_count=2,
        triggered_count=0,
        new_event_count=0,
        failed_count=1,
    )

    with patch("app.services.alerts.evaluate_alert_rules", return_value=summary):
        message = asyncio.run(scheduler._execute(scheduler.tasks["evaluate_alerts"]))

    assert message.endswith("失败 1 条")
    assert scheduler.tasks["evaluate_alerts"].last_status == "degraded"
    assert scheduler.datahub.cache.finished_runs == [("degraded", message)]


def test_task_state_serializes_local_task_runtime_fields() -> None:
    now = datetime(2026, 5, 13, 9, 30, 0)
    task = LocalTask(
        name="check_data_health",
        display_name="检查数据健康",
        interval_seconds=20,
        handler=_handler,
        next_run_at=now + timedelta(seconds=20),
        running=True,
        last_started_at=now,
        last_finished_at=now + timedelta(seconds=1),
        last_status="success",
        last_message="ok",
    )

    state = _task_state(task)

    assert state.name == "check_data_health"
    assert state.running is True
    assert state.last_started_at == "2026-05-13 09:30:00"
    assert state.last_finished_at == "2026-05-13 09:30:01"
    assert state.next_run_at == "2026-05-13 09:30:20"
    assert state.last_message == "ok"


def test_reschedule_task_uses_finished_at_for_automatic_runs() -> None:
    finished_at = datetime(2026, 5, 13, 9, 30, 0)
    task = LocalTask("check_data_health", "检查数据健康", 20, _handler, finished_at)

    _reschedule_task(task, manual=False, finished_at=finished_at)

    assert task.next_run_at == finished_at + timedelta(seconds=20)


def test_reschedule_task_uses_finished_at_for_manual_runs_and_clamps_interval() -> None:
    finished_at = datetime(2026, 5, 13, 9, 30, 0)
    task = LocalTask("check_data_health", "检查数据健康", 0, _handler, finished_at)

    _reschedule_task(task, manual=True, finished_at=finished_at)

    assert task.next_run_at == finished_at + timedelta(seconds=1)


@pytest.mark.parametrize("interval_seconds", [float("inf"), float("nan"), " ", -1])
def test_reschedule_task_clamps_non_finite_or_blank_intervals(interval_seconds) -> None:
    finished_at = datetime(2026, 5, 13, 9, 30, 0)
    task = LocalTask("check_data_health", "检查数据健康", interval_seconds, _handler, finished_at)

    _reschedule_task(task, manual=False, finished_at=finished_at)

    assert task.next_run_at == finished_at + timedelta(seconds=1)


def test_manual_task_failure_raises_after_recording_failed_run() -> None:
    scheduler = LocalDataScheduler(_SchedulerHub())
    scheduler.tasks = {
        "bad_task": LocalTask("bad_task", "失败任务", 20, _failing_handler, datetime.now()),
    }

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(scheduler.run_once("bad_task"))

    assert scheduler.datahub.cache.finished_runs == [("failed", "boom")]
    assert scheduler.datahub.cache.monitor_events[-1] == ("warning", "task", "boom")


def test_task_end_persistence_failures_use_safe_stderr_fallback(capsys) -> None:
    scheduler = LocalDataScheduler(_SchedulerHub(cache=_PersistenceFailingCache()))
    scheduler.tasks = {
        "bad_task": LocalTask("bad_task", "失败任务", 20, _failing_handler, datetime.now()),
    }

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(scheduler.run_once("bad_task"))

    output = capsys.readouterr().err
    assert "scheduler task run persistence failed: OperationalError: database is locked" in output
    assert "scheduler task event persistence failed: OperationalError: readonly database" in output
    assert "task-run-secret" not in output
    assert "event-secret" not in output


def test_task_run_start_failure_clears_runtime_state_and_uses_clean_message() -> None:
    calls: list[str] = []
    cache = _StartFailingCache()
    scheduler = LocalDataScheduler(_SchedulerHub(cache=cache))
    task = LocalTask("bad_task", "失败任务", 20, _recording_handler("handler-ran", calls), datetime.now())
    scheduler.tasks = {"bad_task": task}

    with pytest.raises(RuntimeError, match="database locked"):
        asyncio.run(scheduler.run_once("bad_task"))

    assert calls == []
    assert task.running is False
    assert task.last_status == "failed"
    assert task.last_message == "database locked"
    assert task.last_finished_at is not None
    assert cache.finished_runs == []
    assert cache.monitor_events[-1] == ("warning", "task", "database locked")


def test_automatic_task_failure_returns_message_without_raising() -> None:
    scheduler = LocalDataScheduler(_SchedulerHub())
    task = LocalTask("bad_task", "失败任务", 20, _failing_handler, datetime.now())

    message = asyncio.run(scheduler._execute(task, manual=False))

    assert message == "boom"
    assert task.last_status == "failed"
    assert scheduler.datahub.cache.finished_runs == [("failed", "boom")]


def test_automatic_task_failure_uses_exception_class_for_blank_message() -> None:
    scheduler = LocalDataScheduler(_SchedulerHub())
    task = LocalTask("bad_task", "失败任务", 20, _blank_failing_handler, datetime.now())

    message = asyncio.run(scheduler._execute(task, manual=False))

    assert message == "RuntimeError"
    assert task.last_status == "failed"
    assert scheduler.datahub.cache.finished_runs == [("failed", "RuntimeError")]


def test_cancelled_task_finishes_persisted_run_and_clears_running_state() -> None:
    scheduler = LocalDataScheduler(_SchedulerHub())
    started = asyncio.Event()

    async def blocking_handler() -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    task = LocalTask("slow_task", "慢任务", 20, blocking_handler, datetime.now())

    async def run_check() -> None:
        execution = asyncio.create_task(scheduler._execute(task))
        await started.wait()
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution

    asyncio.run(run_check())

    assert task.running is False
    assert task.last_status == "cancelled"
    assert task.last_message == "慢任务 已取消"
    assert task.last_finished_at is not None
    assert scheduler.datahub.cache.finished_runs == [("cancelled", "慢任务 已取消")]
    assert scheduler.datahub.cache.monitor_events[-1] == ("warning", "task", "慢任务 已取消")


def test_cancelled_task_closes_run_created_after_start_thread_returns() -> None:
    calls: list[str] = []
    cache = _BlockingTaskRunStartCache()
    scheduler = LocalDataScheduler(_SchedulerHub(cache=cache))
    task = LocalTask(
        "late_start",
        "迟到任务",
        20,
        _recording_handler("handler-ran", calls),
        datetime.now(),
    )

    async def run_check() -> None:
        execution = asyncio.create_task(scheduler._execute(task))
        assert await asyncio.to_thread(cache.start_entered.wait, 1)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert cache.run_statuses == {}
        cache.allow_start.set()
        assert await asyncio.to_thread(cache.finished.wait, 1)
        await asyncio.sleep(0)

    asyncio.run(run_check())

    assert calls == []
    assert task.running is False
    assert task.last_status == "cancelled"
    assert cache.run_statuses == {1: "cancelled"}
    assert cache.finished_runs == [("cancelled", "迟到任务 已取消")]
    assert cache.monitor_events[-1] == ("warning", "task", "迟到任务 已取消")


def test_refresh_watch_quotes_normalizes_dedupes_and_skips_invalid_symbols() -> None:
    hub = _SchedulerHub(kline_symbols=["600519", "600519.SH", "bad", " ", "SZ000001"])
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_watch_quotes())

    assert hub.quote_calls == [["600519.SH", "000001.SZ"]]
    assert message == "已刷新 2 只观察个股报价"
    assert ("warning", "quote", "观察池报价刷新剔除 3 个重复或无效股票代码") in hub.cache.monitor_events


def test_refresh_watch_quotes_reports_fallback_cache_as_warning() -> None:
    hub = _SchedulerHub(quote_fallback_symbols={"600519.SH"}, kline_symbols=["600519.SH", "000001.SZ"])
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._execute(scheduler.tasks["refresh_watch_quotes"]))

    assert hub.quote_calls == [["600519.SH", "000001.SZ"]]
    assert message == "已刷新 1 只观察个股报价，兜底缓存 1 只：600519.SH"
    assert scheduler.tasks["refresh_watch_quotes"].last_status == "degraded"
    assert hub.cache.finished_runs == [("degraded", message)]
    assert hub.cache.monitor_events[-1] == ("warning", "quote", message)


def test_all_fallback_quote_refresh_is_persisted_as_degraded() -> None:
    hub = _SchedulerHub(
        quote_fallback_symbols={"600519.SH", "000001.SZ"},
        kline_symbols=["600519.SH", "000001.SZ"],
    )
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._execute(scheduler.tasks["refresh_watch_quotes"]))

    assert message == "已刷新 0 只观察个股报价，兜底缓存 2 只：600519.SH、000001.SZ"
    assert scheduler.tasks["refresh_watch_quotes"].last_status == "degraded"
    assert hub.cache.finished_runs == [("degraded", message)]
    assert hub.cache.monitor_events[-1] == ("warning", "quote", message)


def test_refresh_watch_quotes_reports_partial_provider_coverage() -> None:
    hub = _SchedulerHub(
        kline_symbols=["600519.SH", "000001.SZ"],
        quote_return_symbols=["600519.SH"],
    )
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._execute(scheduler.tasks["refresh_watch_quotes"]))

    assert message == "已刷新 1 只观察个股报价，缺失 1 只：000001.SZ"
    assert scheduler.tasks["refresh_watch_quotes"].last_status == "degraded"
    assert hub.cache.finished_runs == [("degraded", message)]
    assert hub.cache.monitor_events[-1] == ("warning", "quote", message)


def test_refresh_watch_quotes_ignores_duplicate_and_unrequested_rows() -> None:
    hub = _SchedulerHub(
        kline_symbols=["600519.SH", "000001.SZ"],
        quote_return_symbols=["600519.SH", "600519.SH", "300750.SZ"],
    )
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_watch_quotes())

    assert message == "已刷新 1 只观察个股报价，缺失 1 只：000001.SZ"


def test_refresh_watch_quotes_raises_when_all_requested_rows_are_missing() -> None:
    hub = _SchedulerHub(
        kline_symbols=["600519.SH", "000001.SZ"],
        quote_return_symbols=[],
    )
    scheduler = LocalDataScheduler(hub)

    with pytest.raises(RuntimeError, match="观察池报价全部缺失 2 只"):
        asyncio.run(scheduler._refresh_watch_quotes())

    assert hub.cache.monitor_events[-1] == (
        "warning",
        "quote",
        "观察池报价全部缺失 2 只：600519.SH、000001.SZ",
    )


def test_refresh_watch_quotes_returns_skip_message_when_no_valid_symbols_exist() -> None:
    hub = _SchedulerHub(kline_symbols=["bad", " "], seed_symbols=())
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_watch_quotes())

    assert hub.quote_calls == []
    assert message == "无有效观察个股，已跳过报价刷新"
    assert hub.cache.monitor_events[-1] == ("warning", "quote", "无有效观察个股，已跳过报价刷新")


def test_scheduler_selection_uses_only_active_symbols_for_quotes_and_klines() -> None:
    cache = _SelectionSchedulerCache(
        active_symbols=["600001", "600001.SH", "bad", "SZ000002"],
        excluded_symbols=["600519", "600519.SH", "bad"],
        has_entries=True,
    )
    hub = _SchedulerHub(cache=cache, seed_symbols=("600519", "600003"))
    scheduler = LocalDataScheduler(hub)

    quote_message = asyncio.run(scheduler._refresh_watch_quotes())
    kline_message = asyncio.run(scheduler._refresh_key_klines())

    assert hub.quote_calls == [["600001.SH", "000002.SZ"]]
    assert hub.kline_calls == ["600001.SH", "000002.SZ"]
    assert quote_message == "已刷新 2 只观察个股报价"
    assert kline_message == "已刷新 2 只关键个股日K线"
    assert ("warning", "quote", "观察池报价刷新剔除 4 个重复或无效股票代码") in cache.monitor_events
    assert ("warning", "kline", "关键个股K线刷新剔除 4 个重复或无效股票代码") in cache.monitor_events


def test_scheduler_selection_all_excluded_never_falls_back_to_seeds() -> None:
    cache = _SelectionSchedulerCache(
        active_symbols=[],
        excluded_symbols=["600519", "600519.SH", "bad"],
        has_entries=True,
    )
    hub = _SchedulerHub(cache=cache, seed_symbols=("600519", "000001"))
    scheduler = LocalDataScheduler(hub)

    quote_message = asyncio.run(scheduler._refresh_watch_quotes())
    kline_message = asyncio.run(scheduler._refresh_key_klines())

    assert hub.quote_calls == []
    assert hub.kline_calls == []
    assert quote_message == "无有效观察个股，已跳过报价刷新"
    assert kline_message == "无有效关键个股，已跳过日K线刷新"


def test_scheduler_selection_truly_empty_uses_normalized_seeds_for_quotes_and_klines() -> None:
    cache = _SelectionSchedulerCache(active_symbols=[], excluded_symbols=[], has_entries=False)
    hub = _SchedulerHub(
        cache=cache,
        seed_symbols=("600519", "600519.SH", "bad", "000001"),
    )
    scheduler = LocalDataScheduler(hub)

    asyncio.run(scheduler._refresh_watch_quotes())
    asyncio.run(scheduler._refresh_key_klines())

    assert hub.quote_calls == [["600519.SH", "000001.SZ"]]
    assert hub.kline_calls == ["600519.SH", "000001.SZ"]


def test_scheduler_legacy_cache_without_selection_api_keeps_empty_list_seed_fallback() -> None:
    hub = _SchedulerHub(kline_symbols=["bad"], seed_symbols=("600519",))
    scheduler = LocalDataScheduler(hub)

    asyncio.run(scheduler._refresh_watch_quotes())
    asyncio.run(scheduler._refresh_key_klines())

    assert hub.quote_calls == [["600519.SH"]]
    assert hub.kline_calls == ["600519.SH"]


def test_refresh_key_klines_continues_after_per_symbol_failure() -> None:
    hub = _SchedulerHub(kline_failures={"600001.SH"}, kline_symbols=["600001.SH", "600002.SH", "600003.SH"])
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._execute(scheduler.tasks["refresh_key_klines"]))

    assert hub.kline_calls == ["600001.SH", "600002.SH", "600003.SH"]
    assert message == "已刷新 2 只关键个股日K线，失败 1 只"
    assert scheduler.tasks["refresh_key_klines"].last_status == "degraded"
    assert hub.cache.finished_runs == [("degraded", message)]
    assert ("warning", "kline", "关键个股K线刷新失败 1 只：600001.SH: kline failed") in hub.cache.monitor_events


def test_refresh_key_klines_reports_fallback_cache_as_warning() -> None:
    hub = _SchedulerHub(kline_fallback={"600001.SH"}, kline_symbols=["600001.SH", "600002.SH"])
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._execute(scheduler.tasks["refresh_key_klines"]))

    assert hub.kline_calls == ["600001.SH", "600002.SH"]
    assert message == "已刷新 1 只关键个股日K线，兜底缓存 1 只"
    assert scheduler.tasks["refresh_key_klines"].last_status == "degraded"
    assert hub.cache.finished_runs == [("degraded", message)]
    assert hub.cache.monitor_events[-1] == ("warning", "kline", message)


def test_all_fallback_kline_refresh_is_persisted_as_degraded() -> None:
    hub = _SchedulerHub(
        kline_fallback={"600001.SH", "600002.SH"},
        kline_symbols=["600001.SH", "600002.SH"],
    )
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._execute(scheduler.tasks["refresh_key_klines"]))

    assert message == "已刷新 0 只关键个股日K线，兜底缓存 2 只"
    assert scheduler.tasks["refresh_key_klines"].last_status == "degraded"
    assert hub.cache.finished_runs == [("degraded", message)]
    assert hub.cache.monitor_events[-1] == ("warning", "kline", message)


def test_fallback_plate_refresh_is_persisted_as_degraded() -> None:
    hub = _SchedulerHub(plate_fallback=True)
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._execute(scheduler.tasks["refresh_plate_rank"]))

    assert message == "行业背景数据源不可用，使用缓存 1 条"
    assert scheduler.tasks["refresh_plate_rank"].last_status == "degraded"
    assert hub.cache.finished_runs == [("degraded", message)]
    assert hub.cache.monitor_events[-1] == ("warning", "plate", message)


def test_refresh_key_klines_raises_when_all_symbols_fail() -> None:
    hub = _SchedulerHub(kline_failures={"600001.SH", "600002.SH"}, kline_symbols=["600001.SH", "600002.SH"])
    scheduler = LocalDataScheduler(hub)

    with pytest.raises(RuntimeError, match="关键个股日K线全部刷新失败"):
        asyncio.run(scheduler._refresh_key_klines())

    assert hub.kline_calls == ["600001.SH", "600002.SH"]


def test_refresh_key_klines_normalizes_dedupes_and_limits_symbols_after_filtering() -> None:
    hub = _SchedulerHub(
        kline_symbols=["bad", "600001", "600001.SH", "SZ000002", "600003"],
        kline_limit=2,
    )
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_key_klines())

    assert hub.kline_calls == ["600001.SH", "000002.SZ"]
    assert message == "已刷新 2 只关键个股日K线"
    assert ("warning", "kline", "关键个股K线刷新剔除 2 个重复或无效股票代码") in hub.cache.monitor_events


@pytest.mark.parametrize("bad_limit", [-1, float("inf"), " "])
def test_refresh_key_klines_ignores_invalid_symbol_limit_instead_of_skipping_all(bad_limit) -> None:
    hub = _SchedulerHub(kline_symbols=["600001.SH", "600002.SH"])
    hub.settings.scheduler_kline_symbols_limit = bad_limit
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_key_klines())

    assert hub.kline_calls == ["600001.SH", "600002.SH"]
    assert message == "已刷新 2 只关键个股日K线"


def test_refresh_key_klines_counts_empty_results_as_failures() -> None:
    hub = _SchedulerHub(
        kline_empty={"600002.SH"},
        kline_symbols=["600001.SH", "600002.SH", "600003.SH"],
    )
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_key_klines())

    assert hub.kline_calls == ["600001.SH", "600002.SH", "600003.SH"]
    assert message == "已刷新 2 只关键个股日K线，失败 1 只"
    assert ("warning", "kline", "关键个股K线刷新失败 1 只：600002.SH: 返回空K线") in hub.cache.monitor_events


def test_refresh_key_klines_returns_skip_message_when_no_valid_symbols_exist() -> None:
    hub = _SchedulerHub(kline_symbols=[], seed_symbols=())
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_key_klines())

    assert hub.kline_calls == []
    assert message == "无有效关键个股，已跳过日K线刷新"
    assert hub.cache.monitor_events[-1] == ("warning", "kline", "无有效关键个股，已跳过日K线刷新")


def test_data_health_events_prefers_capability_failures_over_provider_names() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    events = _data_health_events(
        _fresh_cache_stats(now),
        [_capability_status("tencent", "quote", healthy=False, last_error="timeout")],
        [_provider_status("akshare", healthy=False)],
        _settings(),
        now=now,
    )

    assert [event.category for event in events] == ["provider"]
    assert events[0].level == "warning"
    assert events[0].message == "数据源最近存在失败：tencent 报价"


def test_data_health_events_deduplicates_and_sorts_capability_failures() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    events = _data_health_events(
        _fresh_cache_stats(now),
        [
            _capability_status("tencent", "quote", healthy=False, last_error="timeout", priority=2),
            _capability_status("akshare", "kline", healthy=False, last_error="timeout", priority=1),
            _capability_status("tencent", "quote", healthy=False, last_error="timeout", priority=2),
        ],
        [],
        _settings(),
        now=now,
    )

    assert [event.message for event in events] == ["数据源最近存在失败：akshare 日K、tencent 报价"]


def test_data_health_events_falls_back_to_provider_failures_when_capabilities_are_inactive() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    events = _data_health_events(
        _fresh_cache_stats(now),
        [_capability_status("tencent", "quote", healthy=False)],
        [_provider_status("akshare", healthy=False)],
        _settings(),
        now=now,
    )

    assert [event.message for event in events] == ["数据源最近存在失败：akshare"]


def test_data_health_events_ignore_stale_provider_failures() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    stale_at = seconds_ago_text(31 * 60)
    events = _data_health_events(
        _fresh_cache_stats(now),
        [_capability_status("tencent", "quote", healthy=False, last_error="timeout", updated_at=stale_at)],
        [_provider_status("akshare", healthy=False, updated_at=stale_at)],
        _settings(),
        now=now,
    )

    assert [(event.level, event.category, event.message) for event in events] == [
        ("info", "health", "报价市场时效、日K市场时效、股票池缓存时效、行业背景缓存时效、股票池可用性、行业背景可用性、数据源状态均正常")
    ]


def test_data_health_events_reports_missing_quote_and_kline_cache() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    events = _data_health_events(
        _cache_stats(latest_quote_at=None, latest_kline_at=None, plate_count=0),
        [],
        [],
        _settings(),
        now=now,
    )

    assert [(event.category, event.message) for event in events] == [
        ("quote", "尚未形成报价缓存或市场事件时间，无法判断报价业务新鲜度。"),
        ("kline", "尚未形成日K缓存或市场日期，无法判断日K业务新鲜度。"),
        ("plate", "尚未形成行业背景缓存。"),
    ]


def test_data_health_events_warns_when_fetch_is_new_but_market_data_is_old() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    fetched_at = "2026-05-13 10:29:30"
    events = _data_health_events(
        _cache_stats(
            latest_quote_at=fetched_at,
            latest_kline_at=fetched_at,
            latest_quote_timestamp="2026-05-12 15:00:00",
            latest_daily_kline_date="2026-05-11",
        ),
        [],
        [],
        _settings(quote_stale_warning_seconds=60, kline_cache_seconds=30),
        now=now,
    )

    assert len(events) == 2
    assert events[0].category == "quote"
    assert "报价市场数据过期" in events[0].message
    assert events[1].category == "kline"
    assert "日K市场数据过期" in events[1].message


def test_data_health_events_reports_stale_daily_kline_even_when_minute_kline_is_fresh() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    fetched_at = "2026-05-13 10:29:30"
    events = _data_health_events(
        _cache_stats(
            latest_quote_at=fetched_at,
            latest_kline_at=fetched_at,
            latest_minute_kline_at=fetched_at,
            latest_quote_timestamp="2026-05-13 10:29:00",
            latest_daily_kline_date="2026-05-11",
            latest_minute_kline_timestamp="2026-05-13 10:29:00",
        ),
        [],
        [],
        _settings(kline_cache_seconds=30),
        now=now,
    )

    assert len(events) == 1
    assert events[0].category == "kline"
    assert "日K市场数据过期" in events[0].message


def test_data_health_events_ignores_legacy_thresholds_for_business_freshness() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    events = _data_health_events(
        _fresh_cache_stats(now),
        [],
        [],
        _settings(quote_stale_warning_seconds=-1, kline_cache_seconds=float("nan")),
        now=now,
    )

    assert [(event.level, event.category, event.message) for event in events] == [
        ("info", "health", "报价市场时效、日K市场时效、股票池缓存时效、行业背景缓存时效、股票池可用性、行业背景可用性均正常")
    ]


def test_data_health_events_reports_future_and_dirty_market_timestamps() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    events = _data_health_events(
        _cache_stats(
            latest_quote_at="2026-05-13 10:29:30",
            latest_kline_at="2026-05-13 10:29:30",
            latest_quote_timestamp="2026-05-14 10:00:00",
            latest_daily_kline_date="dirty-date",
        ),
        [],
        [],
        _settings(),
        now=now,
    )

    assert [(event.category, event.message) for event in events] == [
        ("quote", "报价市场事件时间 2026-05-14 10:00:00 晚于检查时间。"),
        ("kline", "日K市场日期无法解析。"),
    ]


def test_data_health_events_returns_ok_when_no_issue_exists() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    events = _data_health_events(
        _fresh_cache_stats(now),
        [],
        [],
        _settings(),
        now=now,
    )

    assert [(event.level, event.category, event.message) for event in events] == [
        ("info", "health", "报价市场时效、日K市场时效、股票池缓存时效、行业背景缓存时效、股票池可用性、行业背景可用性均正常")
    ]


def test_data_health_events_does_not_claim_missing_industry_background_is_normal() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    stats = _fresh_cache_stats(now).model_copy(update={"plate_count": 0})

    events = _data_health_events(stats, [], [], _settings(), now=now)

    assert [(event.level, event.category, event.message) for event in events] == [("warning", "plate", "尚未形成行业背景缓存。")]
    assert all("正常" not in event.message for event in events)


def test_runtime_cleanup_message_sums_removed_rows() -> None:
    assert _runtime_cleanup_message({"task_run": 2, "monitor_event": 3}) == "已清理 5 条过期运行记录"
    assert _runtime_cleanup_message({"task_run": 3, "monitor_event": -2}) == "已清理 3 条过期运行记录"
    assert _runtime_cleanup_message({"task_run": 0}) is None


def test_runtime_cleanup_message_ignores_non_finite_and_blank_counts() -> None:
    assert (
        _runtime_cleanup_message(
            {
                "task_run": float("inf"),
                "monitor_event": float("nan"),
                "cache_event": " ",
                "alert_event": "2",
            }
        )
        == "已清理 2 条过期运行记录"
    )


def _settings(*, quote_stale_warning_seconds: int = 60, kline_cache_seconds: int = 300):
    return SimpleNamespace(
        quote_stale_warning_seconds=quote_stale_warning_seconds,
        kline_cache_seconds=kline_cache_seconds,
    )


def _scheduler_settings():
    return SimpleNamespace(
        scheduler_quote_interval_seconds=1,
        scheduler_kline_interval_seconds=1,
        scheduler_plate_interval_seconds=1,
        scheduler_health_interval_seconds=1,
    )


def _handlers():
    return {
        "refresh_watch_quotes": _handler,
        "refresh_key_klines": _handler,
        "refresh_plate_rank": _handler,
        "check_data_health": _handler,
        "evaluate_alerts": _handler,
    }


async def _failing_handler() -> str:
    raise RuntimeError("boom")


async def _blank_failing_handler() -> str:
    raise RuntimeError()


def _recording_handler(name: str, calls: list[str]):
    async def handler() -> str:
        calls.append(name)
        return name

    return handler


class _SchedulerCache:
    def __init__(self, kline_symbols: list[str] | None = None) -> None:
        self.kline_symbols = kline_symbols or []
        self.finished_runs: list[tuple[str, str | None]] = []
        self.monitor_events: list[tuple[str, str, str]] = []
        self._next_run_id = 0
        self.orphaned_runs = 0
        self.reconcile_calls = 0

    def reconcile_orphaned_task_runs(self) -> int:
        self.reconcile_calls += 1
        count = self.orphaned_runs
        self.orphaned_runs = 0
        return count

    def start_task_run(self, task_name: str) -> int:
        self._next_run_id += 1
        return self._next_run_id

    def finish_task_run(self, run_id: int, status: str, message: str | None = None) -> None:
        self.finished_runs.append((status, message))

    def save_monitor_event(self, level: str, category: str, message: str, symbol: str | None = None) -> None:
        self.monitor_events.append((level, category, message))

    def watchlist_symbols(self) -> list[str]:
        return list(self.kline_symbols)


class _ScheduledTickScanner:
    def __init__(self) -> None:
        self.calls: list[datetime | None] = []
        self.on_tick = lambda: None

    async def scheduled_tick(self, now: datetime | None = None) -> None:
        self.calls.append(now)
        self.on_tick()


class _StatusMarketScanner:
    def __init__(self, latest=None) -> None:
        self.latest = latest

    def latest_run(self):
        return self.latest


class _SelectionSchedulerCache(_SchedulerCache):
    def __init__(
        self,
        *,
        active_symbols: list[str],
        excluded_symbols: list[str],
        has_entries: bool,
    ) -> None:
        super().__init__()
        self.selection = SimpleNamespace(
            active_symbols=tuple(active_symbols),
            excluded_symbols=tuple(excluded_symbols),
            has_entries=has_entries,
        )

    def watchlist_symbol_selection(self):
        return self.selection


class _ThreadRecordingCache(_SchedulerCache):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_thread_id: int | None = None
        self.maintenance_repo = self

    def stats(self) -> CacheStats:
        return _cache_stats(latest_quote_at=None, latest_kline_at=None)

    def provider_capability_statuses(self) -> list[ProviderCapabilityStatus]:
        return []

    def provider_statuses(self) -> list[ProviderStatus]:
        return []

    def cleanup_regenerable_runtime_rows(self) -> dict[str, int]:
        self.cleanup_thread_id = threading.get_ident()
        return {}

    def cleanup_runtime_rows(self) -> dict[str, int]:
        raise AssertionError("scheduler must not run the full user-history cleanup")


class _ExclusiveGuard:
    def __init__(self) -> None:
        self.acquired = False
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self) -> bool:
        self.acquire_calls += 1
        if self.acquired:
            return False
        self.acquired = True
        return True

    def release(self) -> None:
        assert self.acquired is True
        self.acquired = False
        self.release_calls += 1


class _HeldByOtherGuard:
    def acquire(self) -> bool:
        return False

    def release(self) -> None:
        return None

    def held_by_other(self) -> bool:
        return True


class _BlockingGuard(_ExclusiveGuard):
    def __init__(self) -> None:
        super().__init__()
        self.acquire_started = threading.Event()
        self.allow_acquire = threading.Event()

    def acquire(self) -> bool:
        self.acquire_calls += 1
        self.acquire_started.set()
        self.allow_acquire.wait(timeout=5)
        self.acquired = True
        return True


class _BlockingStartEventCache(_SchedulerCache):
    def __init__(self) -> None:
        super().__init__()
        self.start_event_started = threading.Event()
        self.allow_start_event = threading.Event()

    def save_monitor_event(self, level: str, category: str, message: str, symbol: str | None = None) -> None:
        if category == "scheduler" and message == "本地数据刷新与健康监控已启动":
            self.start_event_started.set()
            self.allow_start_event.wait(timeout=5)
        super().save_monitor_event(level, category, message, symbol=symbol)


class _BlockingTaskRunStartCache(_SchedulerCache):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = threading.Event()
        self.allow_start = threading.Event()
        self.finished = threading.Event()
        self.run_statuses: dict[int, str] = {}

    def start_task_run(self, task_name: str) -> int:
        self.start_entered.set()
        self.allow_start.wait(timeout=5)
        run_id = super().start_task_run(task_name)
        self.run_statuses[run_id] = "running"
        return run_id

    def finish_task_run(self, run_id: int, status: str, message: str | None = None) -> None:
        super().finish_task_run(run_id, status, message)
        self.run_statuses[run_id] = status
        self.finished.set()


class _StartFailingCache(_SchedulerCache):
    def start_task_run(self, task_name: str) -> int:
        raise RuntimeError("  database\nlocked  ")


class _PersistenceFailingCache(_SchedulerCache):
    def finish_task_run(self, run_id: int, status: str, message: str | None = None) -> None:
        raise sqlite3.OperationalError("database is locked; api_key=task-run-secret")

    def save_monitor_event(self, level: str, category: str, message: str, symbol: str | None = None) -> None:
        raise sqlite3.OperationalError("attempt to write a readonly database; token=event-secret")


class _SchedulerHub:
    def __init__(
        self,
        *,
        quote_fallback_symbols: set[str] | None = None,
        quote_return_symbols: list[str] | None = None,
        kline_empty: set[str] | None = None,
        kline_failures: set[str] | None = None,
        kline_fallback: set[str] | None = None,
        kline_limit: int = 10,
        kline_symbols: list[str] | None = None,
        plate_fallback: bool = False,
        cache: _SchedulerCache | None = None,
        seed_symbols: tuple[str, ...] = ("600519.SH",),
    ) -> None:
        self.settings = _scheduler_settings()
        self.settings.scheduler_enabled = False
        self.settings.scheduler_kline_symbols_limit = kline_limit
        self.settings.seed_symbols = seed_symbols
        self.cache = cache or _SchedulerCache(kline_symbols)
        self.quote_fallback_symbols = quote_fallback_symbols or set()
        self.quote_return_symbols = quote_return_symbols
        self.kline_empty = kline_empty or set()
        self.kline_failures = kline_failures or set()
        self.kline_fallback = kline_fallback or set()
        self.kline_calls: list[str] = []
        self.quote_calls: list[list[str]] = []
        self.plate_fallback = plate_fallback
        self.plate_rank_calls: list[tuple[int, bool]] = []

    async def quotes(self, symbols, use_cache: bool = True):
        self.quote_calls.append(list(symbols))
        returned_symbols = list(symbols) if self.quote_return_symbols is None else self.quote_return_symbols
        return [
            SimpleNamespace(code=symbol.split(".")[0], market=symbol.split(".")[1], fallback_used=symbol in self.quote_fallback_symbols)
            for symbol in returned_symbols
        ]

    async def kline(self, symbol: str, limit: int = 120, use_cache: bool = True):
        self.kline_calls.append(symbol)
        if symbol in self.kline_failures:
            raise RuntimeError("kline failed")
        if symbol in self.kline_empty:
            return []
        return [SimpleNamespace(fallback_used=symbol in self.kline_fallback)]

    async def plate_rank_result(self, limit: int = 20, refresh: bool = False):
        self.plate_rank_calls.append((limit, refresh))
        return SimpleNamespace(rows=[SimpleNamespace()], used_fallback_cache=self.plate_fallback)


def _cache_stats(
    *,
    latest_quote_at: str | None,
    latest_kline_at: str | None,
    latest_minute_kline_at: str | None = None,
    latest_quote_timestamp: str | None = None,
    latest_daily_kline_date: str | None = None,
    latest_minute_kline_timestamp: str | None = None,
    stock_count: int = 1,
    plate_count: int = 1,
    metadata_at: str | None = "2026-05-13 10:29:30",
) -> CacheStats:
    return CacheStats(
        path=":memory:",
        quote_count=1 if latest_quote_at else 0,
        quote_history_count=0,
        kline_count=1 if latest_kline_at else 0,
        daily_kline_count=1 if latest_kline_at else 0,
        minute_kline_count=1 if latest_minute_kline_at else 0,
        stock_count=stock_count,
        plate_count=plate_count,
        provider_count=0,
        latest_quote_at=latest_quote_at,
        latest_kline_at=latest_kline_at,
        latest_daily_kline_at=latest_kline_at,
        latest_minute_kline_at=latest_minute_kline_at,
        latest_quote_fetched_at=latest_quote_at,
        latest_daily_kline_fetched_at=latest_kline_at,
        latest_minute_kline_fetched_at=latest_minute_kline_at,
        latest_quote_timestamp=latest_quote_timestamp,
        latest_daily_kline_date=latest_daily_kline_date,
        latest_minute_kline_timestamp=latest_minute_kline_timestamp,
        latest_stock_at=metadata_at if stock_count else None,
        latest_plate_at=metadata_at if plate_count else None,
    )


def _fresh_cache_stats(now: datetime) -> CacheStats:
    fetched_at = (now - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    return _cache_stats(
        latest_quote_at=fetched_at,
        latest_kline_at=fetched_at,
        latest_quote_timestamp=(now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
        latest_daily_kline_date="2026-05-12",
    )


def _capability_status(
    name: str,
    kind: str,
    *,
    healthy: bool,
    last_error: str | None = None,
    priority: int = 1,
    updated_at: str | None = None,
) -> ProviderCapabilityStatus:
    return ProviderCapabilityStatus(
        name=name,
        kind=kind,
        enabled=True,
        priority=priority,
        healthy=healthy,
        last_error=last_error,
        failure_count=1 if last_error else 0,
        updated_at=updated_at,
    )


def _provider_status(name: str, *, healthy: bool, updated_at: str | None = None) -> ProviderStatus:
    return ProviderStatus(
        name=name,
        enabled=True,
        priority=1,
        healthy=healthy,
        last_error=None if healthy else "network down",
        failure_count=0 if healthy else 1,
        updated_at=updated_at,
    )
