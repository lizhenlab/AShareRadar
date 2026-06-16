from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from app.config import get_settings
from app.models.schemas import ScheduledTaskState, SchedulerStatus
from app.services.datahub import DataHub
from app.utils.time import datetime_to_text, seconds_since_text


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


class LocalDataScheduler:
    def __init__(self, datahub: DataHub) -> None:
        self.datahub = datahub
        self.settings = get_settings()
        self.enabled = self.settings.scheduler_enabled
        self.started_at: datetime | None = None
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._active_tasks: set[asyncio.Task[str]] = set()
        now = datetime.now()
        self.tasks: dict[str, LocalTask] = {
            "refresh_watch_quotes": LocalTask(
                name="refresh_watch_quotes",
                display_name="刷新观察池报价",
                interval_seconds=max(10, self.settings.scheduler_quote_interval_seconds),
                handler=self._refresh_watch_quotes,
                next_run_at=now,
            ),
            "refresh_key_klines": LocalTask(
                name="refresh_key_klines",
                display_name="刷新关键个股K线",
                interval_seconds=max(120, self.settings.scheduler_kline_interval_seconds),
                handler=self._refresh_key_klines,
                next_run_at=now + timedelta(seconds=8),
            ),
            "refresh_plate_rank": LocalTask(
                name="refresh_plate_rank",
                display_name="刷新行业背景",
                interval_seconds=max(120, self.settings.scheduler_plate_interval_seconds),
                handler=self._refresh_plate_rank,
                next_run_at=now + timedelta(seconds=12),
            ),
            "check_data_health": LocalTask(
                name="check_data_health",
                display_name="检查数据健康",
                interval_seconds=max(20, self.settings.scheduler_health_interval_seconds),
                handler=self._check_data_health,
                next_run_at=now + timedelta(seconds=16),
            ),
            "evaluate_alerts": LocalTask(
                name="evaluate_alerts",
                display_name="评估本地预警",
                interval_seconds=max(30, self.settings.scheduler_quote_interval_seconds),
                handler=self._evaluate_alerts,
                next_run_at=now + timedelta(seconds=20),
            ),
        }

    async def start(self) -> None:
        if not self.enabled or self._runner and not self._runner.done():
            return
        self.started_at = datetime.now()
        self._stop_event.clear()
        self._runner = asyncio.create_task(self._loop(), name="local-data-scheduler")
        self.datahub.cache.save_monitor_event("info", "scheduler", "本地数据刷新与健康监控已启动")

    async def stop(self) -> None:
        if not self._runner:
            return
        self._stop_event.set()
        await self._runner
        if self._active_tasks:
            active_tasks = list(self._active_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active_tasks, return_exceptions=True),
                    timeout=self.settings.scheduler_shutdown_timeout_seconds,
                )
            except TimeoutError:
                for task in active_tasks:
                    task.cancel()
                await asyncio.gather(*active_tasks, return_exceptions=True)
        self._runner = None
        self.datahub.cache.save_monitor_event("info", "scheduler", "本地数据刷新与健康监控已停止")

    async def run_once(self, task_name: str | None = None) -> list[str]:
        names = [task_name] if task_name else list(self.tasks)
        messages: list[str] = []
        for name in names:
            task = self.tasks.get(name)
            if not task:
                raise ValueError(f"未知任务：{name}")
            messages.append(await self._execute(task, manual=True))
        return messages

    def status(self) -> SchedulerStatus:
        running = bool(self._runner and not self._runner.done())
        return SchedulerStatus(
            enabled=self.enabled,
            running=running,
            started_at=_text_at(self.started_at),
            task_count=len(self.tasks),
            tasks=[
                ScheduledTaskState(
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
                for task in self.tasks.values()
            ],
        )

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now()
            due_tasks = [task for task in self.tasks.values() if not task.running and task.next_run_at <= now]
            for task in due_tasks:
                active_task = asyncio.create_task(self._execute(task), name=f"local-data-task-{task.name}")
                self._active_tasks.add(active_task)
                active_task.add_done_callback(self._active_tasks.discard)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
            except TimeoutError:
                continue

    async def _execute(self, task: LocalTask, manual: bool = False) -> str:
        if task.running:
            return f"{task.display_name} 正在运行，已跳过重复触发"
        task.running = True
        task.last_started_at = datetime.now()
        task.last_finished_at = None
        task.last_status = "running"
        task.last_message = "执行中"
        run_id = self.datahub.cache.start_task_run(task.name)
        try:
            message = await task.handler()
            task.last_status = "success"
            task.last_message = message
            self.datahub.cache.finish_task_run(run_id, "success", message)
            return message
        except Exception as exc:
            message = str(exc)
            task.last_status = "failed"
            task.last_message = message
            self.datahub.cache.finish_task_run(run_id, "failed", message)
            self.datahub.cache.save_monitor_event("warning", "task", message, symbol=None)
            return message
        finally:
            task.running = False
            task.last_finished_at = datetime.now()
            if manual:
                task.next_run_at = datetime.now() + timedelta(seconds=task.interval_seconds)
            else:
                task.next_run_at = task.last_finished_at + timedelta(seconds=task.interval_seconds)

    async def _refresh_watch_quotes(self) -> str:
        symbols = self.datahub.cache.watchlist_symbols() or list(self.settings.seed_symbols)
        quotes = await self.datahub.quotes(symbols, use_cache=False)
        message = f"已刷新 {len(quotes)} 只观察个股报价"
        self.datahub.cache.save_monitor_event("info", "quote", message)
        return message

    async def _refresh_key_klines(self) -> str:
        symbols = (self.datahub.cache.watchlist_symbols() or list(self.settings.seed_symbols))[
            : self.settings.scheduler_kline_symbols_limit
        ]
        refreshed = 0
        for symbol in symbols:
            klines = await self.datahub.kline(symbol, 120, use_cache=False)
            if klines:
                refreshed += 1
            await asyncio.sleep(0)
        message = f"已刷新 {refreshed} 只关键个股日K线"
        self.datahub.cache.save_monitor_event("info", "kline", message)
        return message

    async def _refresh_plate_rank(self) -> str:
        rows = await self.datahub.plate_rank(limit=20, refresh=True)
        message = f"已刷新 {len(rows)} 条行业背景数据"
        self.datahub.cache.save_monitor_event("info", "plate", message)
        return message

    async def _check_data_health(self) -> str:
        stats = self.datahub.cache.stats()
        capability_rows = self.datahub.cache.provider_capability_statuses()
        recent_failures = [
            f"{item.name} {_capability_label(item.kind)}"
            for item in capability_rows
            if item.enabled and not item.healthy and (item.last_error or item.failure_count)
        ]
        if not recent_failures:
            provider_rows = self.datahub.cache.provider_statuses()
            recent_failures = [item.name for item in provider_rows if item.enabled and not item.healthy]
        events: list[str] = []

        if recent_failures:
            message = "数据源最近存在失败：" + "、".join(recent_failures[:5])
            self.datahub.cache.save_monitor_event("warning", "provider", message)
            events.append(message)

        quote_delay = _seconds_since(stats.latest_quote_at)
        if quote_delay is None:
            message = "尚未形成报价缓存，请先打开页面或手动刷新"
            self.datahub.cache.save_monitor_event("warning", "quote", message)
            events.append(message)
        elif quote_delay > self.settings.quote_stale_warning_seconds:
            message = f"报价缓存已超过 {int(quote_delay)} 秒未更新"
            self.datahub.cache.save_monitor_event("warning", "quote", message)
            events.append(message)

        kline_delay = _seconds_since(stats.latest_kline_at)
        if kline_delay is None:
            message = "尚未形成K线缓存，个股趋势分析会依赖实时拉取"
            self.datahub.cache.save_monitor_event("warning", "kline", message)
            events.append(message)
        elif kline_delay > self.settings.kline_cache_seconds * 2:
            message = "K线缓存偏旧，建议手动触发关键个股K线刷新"
            self.datahub.cache.save_monitor_event("warning", "kline", message)
            events.append(message)

        if not events:
            message = "报价、K线、行业背景和数据源状态正常"
            self.datahub.cache.save_monitor_event("info", "health", message)
            events.append(message)

        removed = self.datahub.cache.cleanup_runtime_rows()
        cleanup_total = sum(removed.values())
        if cleanup_total:
            events.append(f"已清理 {cleanup_total} 条过期运行记录")
        return "；".join(events)

    async def _evaluate_alerts(self) -> str:
        from app.services.alerts import evaluate_alert_rules

        summary = await evaluate_alert_rules(self.datahub)
        message = (
            f"已评估 {summary.checked_count} 条本地预警，"
            f"当前触发 {summary.triggered_count} 条，新增事件 {summary.new_event_count} 条"
        )
        level = "warning" if summary.triggered_count else "info"
        self.datahub.cache.save_monitor_event(level, "alert", message)
        return message


def _seconds_since(value: str | None) -> float | None:
    return seconds_since_text(value)


def _capability_label(kind: str) -> str:
    labels = {
        "quote": "报价",
        "kline": "日K",
        "minute": "分钟",
        "stock": "股票池",
        "plate": "板块",
        "concept": "概念",
        "order_book": "盘口",
    }
    return labels.get(kind, kind)
