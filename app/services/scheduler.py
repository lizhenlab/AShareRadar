from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass
from datetime import datetime, timedelta
import fcntl
from functools import partial
import math
import os
from pathlib import Path
import threading
from typing import Awaitable, Callable, Iterable, Protocol, TextIO, TypeVar

from app.models.schemas import (
    CacheStats,
    ProviderCapabilityStatus,
    ProviderStatus,
    ScheduledTaskState,
    SchedulerStatus,
)
from app.services.cache_freshness import assess_cache_freshness
from app.services.datahub import DataHub
from app.services.provider_failure_status import (
    capability_recently_failed as provider_capability_recently_failed,
    provider_recently_failed,
)
from app.services.provider_errors import sanitize_provider_error
from app.utils.symbols import standard_symbol_list
from app.utils.time import datetime_to_text


TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_DEGRADED = "degraded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"
TASK_ERROR_MAX_LENGTH = 120
KLINE_FAILURE_DETAIL_LIMIT = 3
PROVIDER_FAILURE_DETAIL_LIMIT = 5
INSTANCE_GUARD_BUSY_MESSAGE = "已有其他进程运行本地数据调度器，手动任务未执行"
T = TypeVar("T")


class SchedulerInstanceGuard(Protocol):
    def acquire(self) -> bool:
        ...

    def release(self) -> None:
        ...


class NoopSchedulerInstanceGuard:
    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None

    def held_by_other(self) -> bool:
        return False


class FileSchedulerInstanceGuard:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: TextIO | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except Exception:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            raise
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def held_by_other(self) -> bool:
        if self._handle is not None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            handle.close()


class _TaskRunStartHandoff:
    def __init__(self, cache, task_name: str, cancel_message: str) -> None:
        self.cache = cache
        self.task_name = task_name
        self.cancel_message = cancel_message
        self.ready: ConcurrentFuture[int] = ConcurrentFuture()
        self._decision = threading.Event()
        self._cancelled = False

    def run(self) -> None:
        try:
            run_id = self.cache.start_task_run(self.task_name)
        except BaseException as exc:
            self.ready.set_exception(exc)
            return
        self.ready.set_result(run_id)
        self._decision.wait()
        if self._cancelled:
            _finish_task_run_quietly(
                self.cache,
                run_id,
                TASK_STATUS_CANCELLED,
                self.cancel_message,
            )

    def claim(self) -> None:
        self._decision.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._decision.set()


def _text_at(value: datetime | None) -> str | None:
    return datetime_to_text(value)


@dataclass
class LocalTask:
    name: str
    display_name: str
    interval_seconds: int
    handler: Callable[[], Awaitable[str]]
    next_run_at: datetime
    running: bool = False
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_status: str | None = None
    last_message: str | None = None


@dataclass(frozen=True)
class TaskSpec:
    name: str
    display_name: str
    interval_seconds: int
    handler: Callable[[], Awaitable[str]]
    initial_delay_seconds: int = 0


@dataclass(frozen=True)
class TaskDefinition:
    name: str
    display_name: str
    settings_interval_attr: str
    min_interval_seconds: int
    handler_name: str
    initial_delay_seconds: int = 0


@dataclass(frozen=True)
class HealthEvent:
    level: str
    category: str
    message: str


@dataclass(frozen=True)
class KlineRefreshSummary:
    refreshed: int
    fallback_cache: int
    failures: tuple[str, ...]


@dataclass(frozen=True)
class QuoteRefreshSummary:
    requested: int
    refreshed: int
    fallback_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...]

    @property
    def returned(self) -> int:
        return self.refreshed + len(self.fallback_symbols)


class TaskExecutionResult(str):
    """A task message with an explicit persisted outcome, while remaining string-compatible."""

    def __new__(cls, message: str, status: str = TASK_STATUS_SUCCESS):
        result = super().__new__(cls, message)
        result.status = status
        return result


_TASK_DEFINITIONS: tuple[TaskDefinition, ...] = (
    TaskDefinition(
        name="refresh_watch_quotes",
        display_name="刷新观察池报价",
        settings_interval_attr="scheduler_quote_interval_seconds",
        min_interval_seconds=10,
        handler_name="_refresh_watch_quotes",
    ),
    TaskDefinition(
        name="refresh_key_klines",
        display_name="刷新关键个股K线",
        settings_interval_attr="scheduler_kline_interval_seconds",
        min_interval_seconds=120,
        handler_name="_refresh_key_klines",
        initial_delay_seconds=8,
    ),
    TaskDefinition(
        name="refresh_plate_rank",
        display_name="刷新行业背景",
        settings_interval_attr="scheduler_plate_interval_seconds",
        min_interval_seconds=120,
        handler_name="_refresh_plate_rank",
        initial_delay_seconds=12,
    ),
    TaskDefinition(
        name="check_data_health",
        display_name="检查数据健康",
        settings_interval_attr="scheduler_health_interval_seconds",
        min_interval_seconds=20,
        handler_name="_check_data_health",
        initial_delay_seconds=16,
    ),
    TaskDefinition(
        name="evaluate_alerts",
        display_name="评估本地预警",
        settings_interval_attr="scheduler_quote_interval_seconds",
        min_interval_seconds=30,
        handler_name="_evaluate_alerts",
        initial_delay_seconds=20,
    ),
)
_TASK_ORDER = tuple(definition.name for definition in _TASK_DEFINITIONS)

_CAPABILITY_LABELS = {
    "quote": "报价",
    "kline": "日K",
    "minute": "分钟",
    "stock": "股票池",
    "plate": "板块",
    "concept": "概念",
    "order_book": "盘口",
}


class LocalDataScheduler:
    def __init__(
        self,
        datahub: DataHub,
        *,
        instance_guard: SchedulerInstanceGuard | None = None,
    ) -> None:
        self.datahub = datahub
        self.settings = datahub.settings
        self.enabled = self.settings.scheduler_enabled
        self.started_at: datetime | None = None
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._active_tasks: set[asyncio.Task[str]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._manual_run_lock = asyncio.Lock()
        self._instance_guard = instance_guard if instance_guard is not None else _default_instance_guard(datahub)
        self._guard_acquired = False
        self._scheduler_guard_active = False
        self._standby = False
        self._manual_guard_users = 0
        self._shutdown_tasks: set[asyncio.Task] = set()
        self._guard_release_task: asyncio.Task[None] | None = None
        self.tasks = _build_local_tasks(self.settings, datetime.now(), self._task_handlers())

    def _task_handlers(self) -> dict[str, Callable[[], Awaitable[str]]]:
        handlers: dict[str, Callable[[], Awaitable[str]]] = {}
        for definition in _TASK_DEFINITIONS:
            handlers[definition.name] = getattr(self, definition.handler_name)
        return handlers

    async def start(self) -> bool:
        async with self._lifecycle_lock:
            if not self.enabled:
                return False
            if self._shutdown_tasks or self._guard_release_task is not None:
                return False
            if self._runner is not None and not self._runner.done():
                return False
            if self._runner is not None:
                await asyncio.gather(self._runner, return_exceptions=True)
                pending = await self._drain_active_tasks()
                self._runner = None
                if pending:
                    self._scheduler_guard_active = False
                    self._defer_instance_guard_release(pending)
                    return False
            acquired = self._guard_acquired or await self._acquire_instance_guard()
            if not acquired:
                self._standby = True
                await self._save_monitor_event(
                    "info",
                    "scheduler",
                    "已有其他进程运行本地数据调度器，本进程已跳过启动",
                )
                return False
            self._guard_acquired = True
            self._scheduler_guard_active = True
            self._standby = False
            try:
                await self._reconcile_orphaned_task_runs()
                self.started_at = datetime.now()
                self._stop_event.clear()
                self._runner = asyncio.create_task(self._loop(), name="local-data-scheduler")
                self._runner.add_done_callback(_consume_future_exception)
                await self._save_monitor_event("info", "scheduler", "本地数据刷新与健康监控已启动")
            except BaseException:
                await self._abort_start()
                raise
            return True

    async def _reconcile_orphaned_task_runs(self) -> None:
        reconcile = getattr(self.datahub.cache, "reconcile_orphaned_task_runs", None)
        if reconcile is None:
            return
        reconciled = await _offload(reconcile)
        if reconciled:
            await self._save_monitor_event(
                "warning",
                "scheduler",
                f"应用启动时已终止 {reconciled} 条遗留运行记录",
            )

    async def _acquire_instance_guard(self) -> bool:
        acquire = asyncio.create_task(_offload(self._instance_guard.acquire), name="scheduler-instance-guard-acquire")
        try:
            return await asyncio.shield(acquire)
        except asyncio.CancelledError:
            acquired = await asyncio.shield(acquire)
            if acquired:
                await _offload(self._instance_guard.release)
            raise

    async def _abort_start(self) -> None:
        self._stop_event.set()
        runner = self._runner
        shutdown_tasks = tuple(self._active_tasks) + ((runner,) if runner is not None else ())
        pending = await self._wait_for_shutdown_tasks(shutdown_tasks, cancel_first=True)
        self._runner = None
        self.started_at = None
        self._scheduler_guard_active = False
        if pending:
            self._defer_instance_guard_release(pending)
        else:
            await self._release_instance_guard()

    async def stop(self) -> bool:
        cleanup = asyncio.create_task(self._stop(), name="local-data-scheduler-stop")
        cleanup.add_done_callback(_consume_future_exception)
        try:
            return await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.shield(cleanup)
            raise

    async def _stop(self) -> bool:
        async with self._lifecycle_lock:
            if self._shutdown_tasks or self._guard_release_task is not None:
                return False
            runner = self._runner
            if runner is None and not self._scheduler_guard_active and not self._active_tasks:
                return False
            self._stop_event.set()
            shutdown_tasks = tuple(self._active_tasks) + ((runner,) if runner is not None else ())
            pending = set(shutdown_tasks)
            try:
                pending = await self._wait_for_shutdown_tasks(shutdown_tasks)
            finally:
                self._runner = None
                self._scheduler_guard_active = False
                pending = {task for task in pending if not task.done()}
                if pending:
                    self._defer_instance_guard_release(pending)
                else:
                    await self._release_instance_guard()
            await self._save_monitor_event("info", "scheduler", "本地数据刷新与健康监控已停止")
            return True

    async def _drain_active_tasks(self) -> set[asyncio.Task]:
        return await self._wait_for_shutdown_tasks(tuple(self._active_tasks))

    async def _wait_for_shutdown_tasks(
        self,
        tasks: Iterable[asyncio.Task[object]],
        *,
        cancel_first: bool = False,
    ) -> set[asyncio.Task]:
        timeout = _positive_float_or_default(
            getattr(self.settings, "scheduler_shutdown_timeout_seconds", 5.0),
            5.0,
        )
        return await _wait_for_tasks_bounded(tasks, timeout=timeout, cancel_first=cancel_first)

    async def _release_instance_guard(self) -> None:
        if (
            not self._guard_acquired
            or self._scheduler_guard_active
            or self._manual_guard_users
            or self._shutdown_tasks
        ):
            return
        self._guard_acquired = False
        release = asyncio.create_task(_offload(self._instance_guard.release), name="scheduler-instance-guard-release")
        try:
            await asyncio.shield(release)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(release)
            except BaseException:
                self._guard_acquired = True
                raise
            raise
        except BaseException:
            self._guard_acquired = True
            raise

    def _defer_instance_guard_release(self, tasks: Iterable[asyncio.Task]) -> None:
        for task in tasks:
            if task.done() or task in self._shutdown_tasks:
                continue
            self._shutdown_tasks.add(task)
            task.add_done_callback(self._shutdown_task_done)
        if not self._shutdown_tasks:
            self._schedule_instance_guard_release()

    def _shutdown_task_done(self, task: asyncio.Task) -> None:
        _consume_future_exception(task)
        self._shutdown_tasks.discard(task)
        if not self._shutdown_tasks:
            self._schedule_instance_guard_release()

    def _schedule_instance_guard_release(self) -> None:
        if self._guard_release_task is not None:
            return
        release = asyncio.create_task(
            self._release_instance_guard_after_shutdown(),
            name="scheduler-deferred-instance-guard-release",
        )
        self._guard_release_task = release
        release.add_done_callback(_consume_future_exception)

    async def _release_instance_guard_after_shutdown(self) -> None:
        current = asyncio.current_task()
        try:
            async with self._lifecycle_lock:
                if not self._shutdown_tasks:
                    await self._release_instance_guard()
        finally:
            if self._guard_release_task is current:
                self._guard_release_task = None

    async def _save_monitor_event(
        self,
        level: str,
        category: str,
        message: str,
        *,
        symbol: str | None = None,
    ) -> None:
        try:
            await _offload(
                self.datahub.cache.save_monitor_event,
                level,
                category,
                message,
                symbol=symbol,
            )
        except Exception:
            pass

    async def run_once(self, task_name: str | None = None) -> list[str]:
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
            return True

    async def _end_manual_guard_use(self) -> None:
        async with self._lifecycle_lock:
            self._manual_guard_users -= 1
            await self._release_instance_guard()

    def status(self) -> SchedulerStatus:
        running = bool(self._runner and not self._runner.done())
        standby = bool(self.enabled and not running and self._standby and self._standby_lock_is_held())
        return SchedulerStatus(
            enabled=self.enabled,
            running=running,
            standby=standby,
            message=("其他实例持有调度器锁，本进程待命" if standby else None),
            started_at=_text_at(self.started_at),
            task_count=len(self.tasks),
            tasks=[_task_state(task) for task in _ordered_tasks(self.tasks)],
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
            due_tasks = [
                task for task in _ordered_tasks(self.tasks) if not task.running and task.next_run_at <= now
            ]
            for task in due_tasks:
                active_task = asyncio.create_task(self._execute(task), name=f"local-data-task-{task.name}")
                self._active_tasks.add(active_task)
                active_task.add_done_callback(self._active_task_done)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
            except TimeoutError:
                continue

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
            run_id = await _start_task_run_cancel_safe(
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

    async def _refresh_watch_quotes(self) -> str:
        symbols, skipped_count = await _offload(
            _scheduler_cache_symbols,
            self.datahub.cache,
            self.settings.seed_symbols,
        )
        await _offload(_save_symbol_skip_event, self.datahub.cache, "quote", "观察池报价刷新", skipped_count)
        if not symbols:
            message = "无有效观察个股，已跳过报价刷新"
            await self._save_monitor_event("warning", "quote", message)
            return message
        quotes = await self.datahub.quotes(symbols, use_cache=False)
        summary = _quote_refresh_summary(symbols, quotes)
        message = _quote_refresh_message(summary)
        level = "warning" if summary.fallback_symbols or summary.missing_symbols else "info"
        await self._save_monitor_event(level, "quote", message)
        if summary.returned == 0:
            raise RuntimeError(message)
        if summary.fallback_symbols or summary.missing_symbols:
            return TaskExecutionResult(message, TASK_STATUS_DEGRADED)
        return message

    async def _refresh_key_klines(self) -> str:
        symbols, skipped_count = await _offload(
            _scheduler_cache_symbols,
            self.datahub.cache,
            self.settings.seed_symbols,
            limit=self.settings.scheduler_kline_symbols_limit,
        )
        await _offload(_save_symbol_skip_event, self.datahub.cache, "kline", "关键个股K线刷新", skipped_count)
        if not symbols:
            message = "无有效关键个股，已跳过日K线刷新"
            await self._save_monitor_event("warning", "kline", message)
            return message
        summary = await self._refresh_key_kline_symbols(symbols)
        await self._save_kline_refresh_failure_event(summary.failures)
        if summary.failures and summary.refreshed == 0:
            raise RuntimeError(f"关键个股日K线全部刷新失败：{_kline_failure_detail(summary.failures)}")
        message = _kline_refresh_message(summary)
        level = "warning" if summary.failures or summary.fallback_cache else "info"
        await self._save_monitor_event(level, "kline", message)
        if summary.failures or summary.fallback_cache:
            return TaskExecutionResult(message, TASK_STATUS_DEGRADED)
        return message

    async def _refresh_key_kline_symbols(self, symbols: list[str]) -> KlineRefreshSummary:
        refreshed = 0
        fallback_cache = 0
        failures = []
        for symbol in symbols:
            failure = await self._refresh_single_key_kline(symbol)
            if failure is None:
                refreshed += 1
            elif failure == "fallback-cache":
                fallback_cache += 1
            else:
                failures.append(failure)
            await asyncio.sleep(0)
        return KlineRefreshSummary(refreshed=refreshed, fallback_cache=fallback_cache, failures=tuple(failures))

    async def _refresh_single_key_kline(self, symbol: str) -> str | None:
        try:
            klines = await self.datahub.kline(symbol, 120, use_cache=False)
        except Exception as exc:
            return f"{symbol}: {_short_task_error(exc)}"
        if not klines:
            return f"{symbol}: 返回空K线"
        if _rows_used_fallback_cache(klines):
            return "fallback-cache"
        return None

    async def _save_kline_refresh_failure_event(self, failures: tuple[str, ...]) -> None:
        if failures:
            await self._save_monitor_event(
                "warning",
                "kline",
                f"关键个股K线刷新失败 {len(failures)} 只：{_kline_failure_detail(failures)}",
            )

    async def _refresh_plate_rank(self) -> str:
        result = await self.datahub.plate_rank_result(limit=20, refresh=True)
        if result.used_fallback_cache:
            message = f"行业背景数据源不可用，使用缓存 {len(result.rows)} 条"
            await self._save_monitor_event("warning", "plate", message)
            return TaskExecutionResult(message, TASK_STATUS_DEGRADED)
        message = f"已刷新 {len(result.rows)} 条行业背景数据"
        await self._save_monitor_event("info", "plate", message)
        return message

    async def _check_data_health(self, *, now: datetime | None = None) -> str:
        stats, capability_rows, provider_rows = await asyncio.gather(
            _offload(self.datahub.cache.stats),
            _offload(self.datahub.cache.provider_capability_statuses),
            _offload(self.datahub.cache.provider_statuses),
        )
        health_events = _data_health_events(stats, capability_rows, provider_rows, self.settings, now=now)
        for event in health_events:
            await self._save_monitor_event(event.level, event.category, event.message)
        events = [event.message for event in health_events]

        removed = await _offload(self.datahub.cache.maintenance_repo.cleanup_regenerable_runtime_rows)
        if cleanup_message := _runtime_cleanup_message(removed):
            events.append(cleanup_message)
        return "；".join(events)

    async def _evaluate_alerts(self) -> str:
        from app.services.alerts import evaluate_alert_rules

        summary = await evaluate_alert_rules(self.datahub)
        message = (
            f"已评估 {summary.checked_count} 条本地预警，"
            f"当前触发 {summary.triggered_count} 条，新增事件 {summary.new_event_count} 条"
        )
        if summary.failed_count:
            message += f"，失败 {summary.failed_count} 条"
        level = "warning" if summary.triggered_count or summary.failed_count else "info"
        await self._save_monitor_event(level, "alert", message)
        if summary.checked_count and summary.failed_count == summary.checked_count:
            raise RuntimeError(message)
        if summary.failed_count:
            return TaskExecutionResult(message, TASK_STATUS_DEGRADED)
        return message


async def _offload(call: Callable[..., T], *args, **kwargs) -> T:
    return await asyncio.to_thread(partial(call, *args, **kwargs))


async def _start_task_run_cancel_safe(cache, task_name: str, cancel_message: str) -> int:
    loop = asyncio.get_running_loop()
    handoff = _TaskRunStartHandoff(cache, task_name, cancel_message)
    worker = loop.run_in_executor(None, handoff.run)
    worker.add_done_callback(_consume_future_exception)
    ready = asyncio.wrap_future(handoff.ready)
    ready.add_done_callback(_consume_future_exception)
    try:
        run_id = await asyncio.shield(ready)
    except asyncio.CancelledError:
        handoff.cancel()
        raise
    except BaseException:
        handoff.cancel()
        raise
    handoff.claim()
    return run_id


async def _wait_for_tasks_bounded(
    tasks: Iterable[asyncio.Task],
    *,
    timeout: float,
    cancel_first: bool = False,
) -> set[asyncio.Task]:
    pending_tasks = set(tasks)
    if not pending_tasks:
        return set()
    for task in pending_tasks:
        task.add_done_callback(_consume_future_exception)
        if cancel_first:
            task.cancel()
    done, pending = await asyncio.wait(pending_tasks, timeout=timeout)
    for task in done:
        _consume_future_exception(task)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.sleep(0)
    return {task for task in pending if not task.done()}


def _consume_future_exception(future: asyncio.Future) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except asyncio.CancelledError:
        pass


def _default_instance_guard(datahub: DataHub) -> SchedulerInstanceGuard:
    cache_path = getattr(getattr(datahub, "cache", None), "path", None)
    if cache_path is None:
        return NoopSchedulerInstanceGuard()
    return FileSchedulerInstanceGuard(Path(f"{cache_path}.scheduler.lock"))


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
    interval_seconds = _positive_int_at_least(task.interval_seconds, 1)
    task.next_run_at = finished_at + timedelta(seconds=interval_seconds)


def _scheduler_cache_symbols(
    cache,
    seed_symbols: Iterable[object] | None,
    *,
    limit: int | None = None,
) -> tuple[list[str], int]:
    selection_reader = getattr(cache, "watchlist_symbol_selection", None)
    if not callable(selection_reader):
        return _scheduler_symbols(cache.watchlist_symbols(), seed_symbols, limit=limit)
    selection = selection_reader()
    return _scheduler_symbols(
        selection.active_symbols,
        seed_symbols,
        excluded_symbols=selection.excluded_symbols,
        has_entries=selection.has_entries,
        limit=limit,
    )


def _scheduler_symbols(
    watchlist_symbols: Iterable[object] | None,
    seed_symbols: Iterable[object] | None,
    *,
    excluded_symbols: Iterable[object] | None = None,
    has_entries: bool | None = None,
    limit: int | None = None,
) -> tuple[list[str], int]:
    watchlist = list(watchlist_symbols or [])
    if has_entries is not None:
        symbols, skipped_count = _normalize_unique_symbols(watchlist)
        excluded, excluded_skipped_count = _normalize_unique_symbols(excluded_symbols or [])
        skipped_count += excluded_skipped_count
        if symbols:
            return _limit_symbols(symbols, limit), skipped_count
        if has_entries:
            return [], skipped_count
        seeds, seed_skipped_count = _normalize_unique_symbols(seed_symbols or [])
        skipped_count += seed_skipped_count
        excluded_set = set(excluded)
        return _limit_symbols([symbol for symbol in seeds if symbol not in excluded_set], limit), skipped_count

    raw_symbols = watchlist if watchlist else list(seed_symbols or [])
    symbols, skipped_count = _normalize_unique_symbols(raw_symbols)
    if watchlist and not symbols:
        symbols, seed_skipped_count = _normalize_unique_symbols(seed_symbols or [])
        skipped_count += seed_skipped_count
    return _limit_symbols(symbols, limit), skipped_count


def _normalize_unique_symbols(symbols: Iterable[object]) -> tuple[list[str], int]:
    result = standard_symbol_list(symbols, skip_invalid=True, count_duplicates_as_skipped=True)
    return result.symbols, result.skipped_count


def _limit_symbols(symbols: list[str], limit: int | None) -> list[str]:
    limit_count = _positive_int_or_none(limit)
    if limit_count is None:
        return symbols
    return symbols[:limit_count]


def _save_symbol_skip_event(cache, category: str, context: str, skipped_count: int) -> None:
    if skipped_count:
        cache.save_monitor_event(
            "warning",
            category,
            f"{context}剔除 {skipped_count} 个重复或无效股票代码",
        )


def _kline_failure_detail(failures: tuple[str, ...]) -> str:
    return "；".join(failures[:KLINE_FAILURE_DETAIL_LIMIT])


def _quote_refresh_summary(symbols: list[str], quotes: Iterable[object]) -> QuoteRefreshSummary:
    requested_symbols = tuple(dict.fromkeys(symbols))
    requested_set = set(requested_symbols)
    returned: dict[str, object] = {}
    for quote in quotes:
        symbol = _quote_symbol(quote)
        if symbol in requested_set:
            returned[symbol] = quote
    fallback_symbols = tuple(symbol for symbol in requested_symbols if symbol in returned and _item_used_fallback_cache(returned[symbol]))
    missing_symbols = tuple(symbol for symbol in requested_symbols if symbol not in returned)
    return QuoteRefreshSummary(
        requested=len(requested_symbols),
        refreshed=len(returned) - len(fallback_symbols),
        fallback_symbols=fallback_symbols,
        missing_symbols=missing_symbols,
    )


def _quote_refresh_message(summary: QuoteRefreshSummary) -> str:
    if summary.returned == 0:
        return f"观察池报价全部缺失 {summary.requested} 只：{_quote_missing_detail(summary.missing_symbols)}"
    message = f"已刷新 {summary.refreshed} 只观察个股报价"
    if summary.fallback_symbols:
        message += f"，兜底缓存 {len(summary.fallback_symbols)} 只：{_quote_fallback_detail(summary.fallback_symbols)}"
    if summary.missing_symbols:
        message += f"，缺失 {len(summary.missing_symbols)} 只：{_quote_missing_detail(summary.missing_symbols)}"
    return message


def _quote_fallback_detail(symbols: tuple[str, ...]) -> str:
    return "、".join(symbols[:PROVIDER_FAILURE_DETAIL_LIMIT])


def _quote_missing_detail(symbols: tuple[str, ...]) -> str:
    return "、".join(symbols[:PROVIDER_FAILURE_DETAIL_LIMIT])


def _kline_refresh_message(summary: KlineRefreshSummary) -> str:
    message = f"已刷新 {summary.refreshed} 只关键个股日K线"
    if summary.fallback_cache:
        message += f"，兜底缓存 {summary.fallback_cache} 只"
    if summary.failures:
        message += f"，失败 {len(summary.failures)} 只"
    return message


def _quote_symbol(item: object) -> str:
    code = str(getattr(item, "code", "") or "").strip()
    market = str(getattr(item, "market", "") or "").strip()
    raw = f"{code}.{market}" if code and market else ""
    symbols = standard_symbol_list([raw], skip_invalid=True).symbols
    return symbols[0] if symbols else "--"


def _rows_used_fallback_cache(rows: Iterable[object]) -> bool:
    return any(_item_used_fallback_cache(item) for item in rows)


def _item_used_fallback_cache(item: object) -> bool:
    return bool(getattr(item, "fallback_used", False))


def _data_health_events(
    stats: CacheStats,
    capability_rows: list[ProviderCapabilityStatus],
    provider_rows: list[ProviderStatus],
    _settings,
    *,
    now: datetime | None = None,
) -> list[HealthEvent]:
    assessment = assess_cache_freshness(
        stats,
        now=now or datetime.now(),
        stock_pool_cache_seconds=getattr(_settings, "stock_pool_cache_seconds", 24 * 60 * 60),
        plate_rank_cache_seconds=getattr(_settings, "plate_rank_cache_seconds", 10 * 60),
    )
    events = [
        *_provider_health_events(capability_rows, provider_rows),
        *(
            HealthEvent("warning", issue.category, issue.message)
            for issue in assessment.issues
        ),
    ]
    if events:
        return events
    checked_domains = list(assessment.checked_domains)
    if capability_rows or provider_rows:
        checked_domains.append("数据源状态")
    return [HealthEvent("info", "health", f"{'、'.join(checked_domains)}均正常")]


def _provider_health_events(
    capability_rows: list[ProviderCapabilityStatus],
    provider_rows: list[ProviderStatus],
) -> list[HealthEvent]:
    failures = _recent_provider_failures(capability_rows, provider_rows)
    if not failures:
        return []
    return [
        HealthEvent(
            "warning",
            "provider",
            "数据源最近存在失败：" + "、".join(failures[:PROVIDER_FAILURE_DETAIL_LIMIT]),
        )
    ]


def _recent_provider_failures(
    capability_rows: list[ProviderCapabilityStatus],
    provider_rows: list[ProviderStatus],
) -> list[str]:
    capability_failures = _recent_capability_failures(capability_rows)
    if capability_failures:
        return capability_failures
    return _unhealthy_provider_failures(provider_rows)


def _recent_capability_failures(capability_rows: list[ProviderCapabilityStatus]) -> list[str]:
    return _unique_texts(
        f"{item.name} {_capability_label(item.kind)}"
        for item in sorted(capability_rows, key=_capability_sort_key)
        if provider_capability_recently_failed(item)
    )


def _unhealthy_provider_failures(provider_rows: list[ProviderStatus]) -> list[str]:
    return _unique_texts(
        item.name for item in sorted(provider_rows, key=_provider_sort_key) if provider_recently_failed(item)
    )


def _runtime_cleanup_message(removed: dict[str, int]) -> str | None:
    cleanup_total = sum(_positive_int_or_zero(count) for count in removed.values())
    if not cleanup_total:
        return None
    return f"已清理 {cleanup_total} 条过期运行记录"


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


def _positive_float_or_default(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _task_error_message(exc: Exception) -> str:
    text = " ".join(sanitize_provider_error(exc).strip().split())
    if not text:
        return exc.__class__.__name__
    return text[:TASK_ERROR_MAX_LENGTH]


def _record_task_end(cache, run_id: int | None, status: str, message: str) -> None:
    if run_id is not None:
        _finish_task_run_quietly(cache, run_id, status, message)
    try:
        cache.save_monitor_event("warning", "task", message, symbol=None)
    except Exception:
        pass


def _finish_task_run_quietly(cache, run_id: int, status: str, message: str) -> None:
    try:
        cache.finish_task_run(run_id, status, message)
    except Exception:
        pass


def _short_task_error(exc: Exception) -> str:
    return _task_error_message(exc)


def _provider_sort_key(item: ProviderStatus) -> tuple[int, str]:
    return (item.priority, item.name.casefold())


def _capability_sort_key(item: ProviderCapabilityStatus) -> tuple[int, str, str]:
    return (item.priority, item.name.casefold(), item.kind.casefold())


def _unique_texts(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _capability_label(kind: str) -> str:
    return _CAPABILITY_LABELS.get(kind, kind)
