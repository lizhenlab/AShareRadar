from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta

from app.models.market_scan import MarketScanRun
from app.models.schemas import ScheduledTaskState, SchedulerStatus
from app.services.market_scan_manager import MARKET_SCAN_TASK_LABEL, MARKET_SCAN_TASK_NAME
from app.services.scheduler_contracts import (
    INSTANCE_GUARD_BUSY_MESSAGE,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    LocalTask,
    SchedulerRuntimeContext,
    _text_at,
)
from app.services.scheduler_helpers import (
    _consume_future_exception,
    _offload,
    _record_task_end,
    _short_task_error,
    _task_error_message,
)
from app.services.scheduler_schedule import (
    _ordered_task_names,
    _ordered_tasks,
    _reschedule_task,
    _task_result_status,
    _task_state,
)
from app.services.task_run_lifecycle import start_task_run_cancel_safe
from app.services.trading_calendar import DAILY_KLINE_PUBLISH_TIME, is_trading_day
from app.utils.market_time import market_local_naive, market_now_naive


_MARKET_SCAN_SCHEDULE_CONSUMED_STATUSES = frozenset(
    {"queued", "running", "cancelling", "success", "degraded", "cancelled"}
)
_MARKET_SCAN_SCHEDULE_LOOKAHEAD_DAYS = 370


class SchedulerExecutionMixin(SchedulerRuntimeContext):
    async def run_once(self, task_name: str | None = None) -> list[str]:
        if task_name == MARKET_SCAN_TASK_NAME:
            if self.market_scanner is None:
                raise RuntimeError("全市场扫描管理器未启用")
            response = await self.market_scanner.create_scan(trigger="manual")
            status = "已在运行" if response.deduplicated else "已创建"
            return [f"{MARKET_SCAN_TASK_LABEL}{status}：批次 {response.run.id}"]
        names = [task_name] if task_name else _ordered_task_names(self.tasks)
        for name in names:
            if name not in self.tasks:
                raise ValueError(f"未知任务：{name}")
        async with self._manual_run_lock:
            acquired = await self._begin_manual_guard_use()
            if not acquired:
                await self._save_monitor_event("info", "scheduler", INSTANCE_GUARD_BUSY_MESSAGE)
                raise RuntimeError(INSTANCE_GUARD_BUSY_MESSAGE)
            try:
                messages: list[str] = []
                for name in names:
                    messages.append(await self._execute(self.tasks[name], manual=True))
                return messages
            finally:
                await self._end_manual_guard_use()

    async def _begin_manual_guard_use(self) -> bool:
        async with self._lifecycle_lock:
            if not self._guard_acquired:
                if not await self._acquire_instance_guard():
                    return False
                self._guard_acquired = True
            self._manual_guard_users += 1
            self._quiescent_event.clear()
            return True

    async def _end_manual_guard_use(self) -> None:
        async with self._lifecycle_lock:
            self._manual_guard_users -= 1
            await self._release_instance_guard()
            self._mark_quiescent_if_idle()

    def status(self) -> SchedulerStatus:
        running = bool(self._runner and not self._runner.done())
        standby = bool(self.enabled and not running and self._standby and self._standby_lock_is_held())
        task_states = [_task_state(task) for task in _ordered_tasks(self.tasks)]
        if self.market_scanner is not None:
            task_states.append(
                self._market_scan_task_state(
                    now=market_now_naive(),
                    schedule_available=running or standby,
                )
            )
        return SchedulerStatus(
            enabled=self.enabled,
            running=running,
            standby=standby,
            message=("其他实例持有调度器锁，本进程待命" if standby else None),
            started_at=_text_at(self.started_at),
            task_count=len(task_states),
            tasks=task_states,
        )

    def _market_scan_task_state(
        self,
        *,
        now: datetime,
        schedule_available: bool,
    ) -> ScheduledTaskState:
        latest = self.market_scanner.latest_run() if self.market_scanner is not None else None
        automatic_enabled = bool(self.settings.market_scan_auto_enabled)
        next_run_at = None
        if automatic_enabled and self.enabled and schedule_available:
            next_run_at = _next_market_scan_run_at(self.settings, latest, now)
        return ScheduledTaskState(
            name=MARKET_SCAN_TASK_NAME,
            display_name=MARKET_SCAN_TASK_LABEL,
            interval_seconds=24 * 60 * 60,
            running=bool(latest and latest.status in {"queued", "running", "cancelling"}),
            automatic_enabled=automatic_enabled,
            last_started_at=latest.started_at if latest else None,
            last_finished_at=latest.finished_at if latest else None,
            next_run_at=_text_at(next_run_at),
            last_status=latest.status if latest else None,
            last_message=latest.message if latest else None,
        )

    def _standby_lock_is_held(self) -> bool:
        probe = getattr(self._instance_guard, "held_by_other", None)
        if not callable(probe):
            return True
        try:
            return bool(probe())
        except OSError:
            return False

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now()
            await self._tick_market_scan()
            due_tasks = [task for task in _ordered_tasks(self.tasks) if not task.running and task.next_run_at <= now]
            for task in due_tasks:
                active_task = asyncio.create_task(self._execute(task), name=f"local-data-task-{task.name}")
                self._active_tasks.add(active_task)
                active_task.add_done_callback(self._active_task_done)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
            except TimeoutError:
                continue

    async def _tick_market_scan(self, now: datetime | None = None) -> None:
        if self.market_scanner is None:
            return
        try:
            await self.market_scanner.scheduled_tick(now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._save_monitor_event(
                "warning",
                "market_scan",
                f"全市场自动扫描调度失败：{_short_task_error(exc)}",
            )

    def _active_task_done(self, task: asyncio.Task[str]) -> None:
        self._active_tasks.discard(task)
        _consume_future_exception(task)

    async def _execute(self, task: LocalTask, manual: bool = False) -> str:
        if task.running:
            return f"{task.display_name} 正在运行，已跳过重复触发"
        task.running = True
        task.last_started_at = datetime.now()
        task.last_finished_at = None
        task.last_status = TASK_STATUS_RUNNING
        task.last_message = "执行中"
        run_id: int | None = None
        try:
            run_id = await start_task_run_cancel_safe(
                self.datahub.cache,
                task.name,
                f"{task.display_name} 已取消",
            )
            result = await task.handler()
            message = str(result)
            status = _task_result_status(result)
            task.last_status = status
            task.last_message = message
            await _offload(self.datahub.cache.finish_task_run, run_id, status, message)
            return message
        except asyncio.CancelledError:
            message = f"{task.display_name} 已取消"
            task.last_status = TASK_STATUS_CANCELLED
            task.last_message = message
            await _offload(_record_task_end, self.datahub.cache, run_id, TASK_STATUS_CANCELLED, message)
            raise
        except Exception as exc:
            message = _task_error_message(exc)
            task.last_status = TASK_STATUS_FAILED
            task.last_message = message
            await _offload(_record_task_end, self.datahub.cache, run_id, TASK_STATUS_FAILED, message)
            if manual:
                raise RuntimeError(message) from exc
            return message
        finally:
            task.running = False
            finished_at = datetime.now()
            task.last_finished_at = finished_at
            _reschedule_task(task, manual, finished_at)


def _next_market_scan_run_at(
    settings: object,
    latest: MarketScanRun | None,
    now: datetime,
) -> datetime | None:
    current = market_local_naive(now)
    configured_time = time(
        int(getattr(settings, "market_scan_schedule_hour")),
        int(getattr(settings, "market_scan_schedule_minute")),
    )
    schedule_time = max(configured_time, DAILY_KLINE_PUBLISH_TIME)
    candidate_date = current.date()
    for _ in range(_MARKET_SCAN_SCHEDULE_LOOKAHEAD_DAYS):
        if is_trading_day(candidate_date):
            candidate = datetime.combine(candidate_date, schedule_time)
            if not _market_scan_schedule_consumed(latest, candidate_date):
                return current if candidate < current else candidate
        candidate_date += timedelta(days=1)
    return None


def _market_scan_schedule_consumed(latest: MarketScanRun | None, data_date: date) -> bool:
    if latest is None or latest.data_date != data_date.isoformat():
        return False
    return latest.status in _MARKET_SCAN_SCHEDULE_CONSUMED_STATUSES or latest.trigger in {"scheduled", "retry"}
