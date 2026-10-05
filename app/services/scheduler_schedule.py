from __future__ import annotations

from datetime import datetime, time, timedelta
import math
from typing import Any, Awaitable, Callable

from app.models.system import (
    ScheduledTaskState,
)
from app.services.scheduler_contracts import (
    TASK_STATUS_DEGRADED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_SUCCESS,
    LocalTask,
    TaskDefinition,
    TaskSpec,
    TaskWindow,
    _TASK_DEFINITIONS,
    _TASK_ORDER,
    _text_at,
)
from app.services.trading_calendar import (
    AFTERNOON_SESSION_START_TIME,
    CALL_AUCTION_START_TIME,
    DAILY_KLINE_PUBLISH_TIME,
    MORNING_SESSION_END_TIME,
    TradingCalendarCoverageError,
    next_trade_dates,
    trading_dates_between,
)
from app.utils.market_time import market_local_naive


_AUTOMATIC_WINDOWS: dict[TaskWindow, tuple[tuple[time, time], ...]] = {
    "market": ((CALL_AUCTION_START_TIME, MORNING_SESSION_END_TIME),
               (AFTERNOON_SESSION_START_TIME, DAILY_KLINE_PUBLISH_TIME)),
    "daytime": ((CALL_AUCTION_START_TIME, MORNING_SESSION_END_TIME),
                (AFTERNOON_SESSION_START_TIME, time(20))),
    "post_close": ((DAILY_KLINE_PUBLISH_TIME, time(20)),),
    "stock_metadata": ((time(8, 30), CALL_AUCTION_START_TIME),
                       (DAILY_KLINE_PUBLISH_TIME, time(20))),
}
_CALENDAR_RECHECK_DELAY = timedelta(hours=6)
_CALENDAR_WARNING = "可信交易日历覆盖不足，自动执行暂缓；至少6小时后重新检查，手动执行仍可用"
_BACKOFF_MAX_SECONDS = 3600
_HEALTH_DAYTIME_MIN_SECONDS = 300
_HEALTH_OFF_HOURS_MIN_SECONDS = 900


def _build_local_tasks(
    settings,
    now: datetime,
    handlers: dict[str, Callable[[], Awaitable[str]]],
) -> dict[str, LocalTask]:
    return {spec.name: _local_task_from_spec(spec, now) for spec in _task_specs(settings, handlers)}


def _local_task_from_spec(spec: TaskSpec, now: datetime) -> LocalTask:
    task = LocalTask(
        name=spec.name,
        display_name=spec.display_name,
        interval_seconds=spec.interval_seconds,
        handler=spec.handler,
        next_run_at=market_local_naive(now) + timedelta(seconds=spec.initial_delay_seconds),
        automatic_window=spec.automatic_window,
    )
    task.next_run_at = _next_automatic_run_at(task, task.next_run_at)
    return task


def _task_specs(settings, handlers: dict[str, Callable[[], Awaitable[str]]]) -> tuple[TaskSpec, ...]:
    return tuple(_task_spec_from_definition(settings, handlers, definition) for definition in _TASK_DEFINITIONS)


def _task_spec_from_definition(
    settings,
    handlers: dict[str, Callable[[], Awaitable[str]]],
    definition: TaskDefinition,
) -> TaskSpec:
    interval = _positive_int_at_least(
        getattr(settings, definition.settings_interval_attr) if definition.settings_interval_attr else None,
        definition.min_interval_seconds,
    )
    delay = definition.initial_delay_seconds
    if definition.automatic_window == "maintenance":
        delay = max(delay, interval)
    return TaskSpec(
        name=definition.name,
        display_name=definition.display_name,
        interval_seconds=interval,
        handler=handlers[definition.name],
        initial_delay_seconds=delay,
        automatic_window=definition.automatic_window,
    )


def _ordered_task_names(tasks: dict[str, LocalTask]) -> list[str]:
    known_names = [name for name in _TASK_ORDER if name in tasks]
    unknown_names = sorted(name for name in tasks if name not in _TASK_ORDER)
    return [*known_names, *unknown_names]


def _ordered_tasks(tasks: dict[str, LocalTask]) -> list[LocalTask]:
    return [tasks[name] for name in _ordered_task_names(tasks)]


def _task_state(task: LocalTask) -> ScheduledTaskState:
    return ScheduledTaskState(
        name=task.name,
        display_name=task.display_name,
        interval_seconds=task.interval_seconds,
        running=task.running,
        last_started_at=_text_at(task.last_started_at),
        last_finished_at=_text_at(task.last_finished_at),
        next_run_at=_text_at(task.next_run_at),
        last_status=task.last_status,
        last_message=_schedule_message(task),
    )


def _task_result_status(result: object) -> str:
    status = getattr(result, "status", TASK_STATUS_SUCCESS)
    return status if status in {TASK_STATUS_SUCCESS, TASK_STATUS_DEGRADED, TASK_STATUS_PENDING} else TASK_STATUS_SUCCESS


def _reschedule_task(task: LocalTask, manual: bool, finished_at: datetime) -> None:
    del manual
    current = market_local_naive(finished_at)
    if task.last_status in {TASK_STATUS_FAILED, TASK_STATUS_DEGRADED}:
        task.consecutive_failures += 1
    elif task.last_status in {TASK_STATUS_SUCCESS, TASK_STATUS_PENDING}:
        task.consecutive_failures = 0
    interval = _automatic_interval(task, current)
    task.next_run_at = _next_automatic_run_at(task, current + timedelta(seconds=interval))


def _schedule_message(task: LocalTask) -> str | None:
    parts = [task.last_message] if task.last_message else []
    if task.consecutive_failures:
        parts.append(f"连续失败或降级 {task.consecutive_failures} 次，自动重试已退避")
    if task.schedule_warning:
        parts.append(task.schedule_warning)
    return "；".join(parts) or None


def _automatic_interval(task: LocalTask, current: datetime) -> int:
    interval = _positive_int_at_least(task.interval_seconds, 1)
    if task.automatic_window == "health":
        in_daytime = _in_health_working_window(current)
        minimum = _HEALTH_DAYTIME_MIN_SECONDS if in_daytime else _HEALTH_OFF_HOURS_MIN_SECONDS
        interval = max(interval, minimum)
    ceiling = max(interval, _BACKOFF_MAX_SECONDS)
    return min(ceiling, interval * (2 ** min(12, task.consecutive_failures)))


def _in_health_working_window(current: datetime) -> bool:
    try:
        days = trading_dates_between(current.date(), current.date(), allow_auto_refresh=False)
    except TradingCalendarCoverageError:
        return False
    return bool(days) and any(start <= current.time() < end for start, end in _AUTOMATIC_WINDOWS["daytime"])


def _next_automatic_run_at(task: LocalTask, earliest: datetime) -> datetime:
    current = market_local_naive(earliest)
    if task.automatic_window == "maintenance":
        task.schedule_warning = None
        # Local cache retention needs a quiet clock window, not market data or
        # calendar refresh. Weekends and calendar gaps remain maintainable.
        if time(9) <= current.time() < time(20):
            return datetime.combine(current.date(), time(20))
        return current
    if task.automatic_window == "always":
        return current
    try:
        planned = _next_window(task.automatic_window, current)
    except TradingCalendarCoverageError:
        task.schedule_warning = _CALENDAR_WARNING
        return current + _CALENDAR_RECHECK_DELAY
    task.schedule_warning = None
    return planned


def _next_window(window: TaskWindow, current: datetime) -> datetime:
    days = trading_dates_between(current.date(), current.date(), allow_auto_refresh=False)
    if window == "health":
        return current
    windows = _AUTOMATIC_WINDOWS[window]
    if days:
        for start, end in windows:
            opening, closing = datetime.combine(current.date(), start), datetime.combine(current.date(), end)
            if current < closing:
                return max(current, opening)
    next_day = next_trade_dates(current.date(), 1, allow_auto_refresh=False)[0]
    return datetime.combine(next_day, windows[0][0])


def automatic_task_due(task: LocalTask, now: datetime) -> bool:
    """Recheck trusted session admission without creating a skipped execution record."""
    current = market_local_naive(now)
    if task.running or market_local_naive(task.next_run_at) > current:
        return False
    task.next_run_at = _next_automatic_run_at(task, current)
    return task.next_run_at <= current


def _positive_int_at_least(value: object, minimum: int) -> int:
    parsed = _positive_int_or_none(value)
    if parsed is None:
        return minimum
    return max(minimum, parsed)


def _positive_int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value) or None
    try:
        number = float(value.strip() if isinstance(value, str) else str(value))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return max(1, int(number))


def _positive_int_or_zero(value: object) -> int:
    return _positive_int_or_none(value) or 0


def _positive_float_or_default(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default
