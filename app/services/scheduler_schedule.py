from __future__ import annotations

from datetime import datetime, timedelta
import math
from typing import Any, Awaitable, Callable

from app.models.schemas import ScheduledTaskState
from app.services.scheduler_contracts import (
    TASK_STATUS_DEGRADED,
    TASK_STATUS_SUCCESS,
    LocalTask,
    TaskDefinition,
    TaskSpec,
    _TASK_DEFINITIONS,
    _TASK_ORDER,
    _text_at,
)


def _build_local_tasks(
    settings,
    now: datetime,
    handlers: dict[str, Callable[[], Awaitable[str]]],
) -> dict[str, LocalTask]:
    return {spec.name: _local_task_from_spec(spec, now) for spec in _task_specs(settings, handlers)}


def _local_task_from_spec(spec: TaskSpec, now: datetime) -> LocalTask:
    return LocalTask(
        name=spec.name,
        display_name=spec.display_name,
        interval_seconds=spec.interval_seconds,
        handler=spec.handler,
        next_run_at=now + timedelta(seconds=spec.initial_delay_seconds),
    )


def _task_specs(settings, handlers: dict[str, Callable[[], Awaitable[str]]]) -> tuple[TaskSpec, ...]:
    return tuple(_task_spec_from_definition(settings, handlers, definition) for definition in _TASK_DEFINITIONS)


def _task_spec_from_definition(
    settings,
    handlers: dict[str, Callable[[], Awaitable[str]]],
    definition: TaskDefinition,
) -> TaskSpec:
    return TaskSpec(
        name=definition.name,
        display_name=definition.display_name,
        interval_seconds=_positive_int_at_least(
            getattr(settings, definition.settings_interval_attr),
            definition.min_interval_seconds,
        ),
        handler=handlers[definition.name],
        initial_delay_seconds=definition.initial_delay_seconds,
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
        last_message=task.last_message,
    )


def _task_result_status(result: object) -> str:
    status = getattr(result, "status", TASK_STATUS_SUCCESS)
    return status if status in {TASK_STATUS_SUCCESS, TASK_STATUS_DEGRADED} else TASK_STATUS_SUCCESS


def _reschedule_task(task: LocalTask, manual: bool, finished_at: datetime) -> None:
    del manual
    interval_seconds = _positive_int_at_least(task.interval_seconds, 1)
    task.next_run_at = finished_at + timedelta(seconds=interval_seconds)


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
