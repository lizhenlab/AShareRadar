from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Awaitable, Callable, Iterable

from app.models.schemas import (
    CacheStats,
    ProviderCapabilityStatus,
    ProviderStatus,
    ScheduledTaskState,
    SchedulerStatus,
)
from app.services.datahub import DataHub
from app.services.provider_failure_status import (
    capability_recently_failed as provider_capability_recently_failed,
    provider_recently_failed,
)
from app.utils.symbols import standard_symbol_list
from app.utils.time import datetime_to_text, non_negative_seconds_since_text


TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"
TASK_ERROR_MAX_LENGTH = 120
KLINE_FAILURE_DETAIL_LIMIT = 3
PROVIDER_FAILURE_DETAIL_LIMIT = 5


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


@dataclass(frozen=True)
class CacheFreshnessRule:
    category: str
    timestamp_attr: str
    threshold_attr: str
    threshold_multiplier: int
    missing_message: str
    invalid_message: str
    stale_message: Callable[[int], str]


def _quote_stale_message(delay_seconds: int) -> str:
    return f"报价缓存已超过 {delay_seconds} 秒未更新"


def _kline_stale_message(delay_seconds: int) -> str:
    return "日K缓存偏旧，建议手动触发关键个股K线刷新"


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

_QUOTE_CACHE_RULE = CacheFreshnessRule(
    category="quote",
    timestamp_attr="latest_quote_at",
    threshold_attr="quote_stale_warning_seconds",
    threshold_multiplier=1,
    missing_message="尚未形成报价缓存，请先打开页面或手动刷新",
    invalid_message="报价缓存时间异常，需检查系统时间或缓存数据",
    stale_message=_quote_stale_message,
)
_KLINE_CACHE_RULE = CacheFreshnessRule(
    category="kline",
    timestamp_attr="latest_kline_at",
    threshold_attr="kline_cache_seconds",
    threshold_multiplier=2,
    missing_message="尚未形成日K缓存，个股趋势分析会依赖实时拉取",
    invalid_message="日K缓存时间异常，需检查系统时间或缓存数据",
    stale_message=_kline_stale_message,
)
_CACHE_FRESHNESS_RULES = (_QUOTE_CACHE_RULE, _KLINE_CACHE_RULE)

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
    def __init__(self, datahub: DataHub) -> None:
        self.datahub = datahub
        self.settings = datahub.settings
        self.enabled = self.settings.scheduler_enabled
        self.started_at: datetime | None = None
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._active_tasks: set[asyncio.Task[str]] = set()
        self.tasks = _build_local_tasks(self.settings, datetime.now(), self._task_handlers())

    def _task_handlers(self) -> dict[str, Callable[[], Awaitable[str]]]:
        handlers: dict[str, Callable[[], Awaitable[str]]] = {}
        for definition in _TASK_DEFINITIONS:
            handlers[definition.name] = getattr(self, definition.handler_name)
        return handlers

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
        names = [task_name] if task_name else _ordered_task_names(self.tasks)
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
            tasks=[_task_state(task) for task in _ordered_tasks(self.tasks)],
        )

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now()
            due_tasks = [
                task for task in _ordered_tasks(self.tasks) if not task.running and task.next_run_at <= now
            ]
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
        task.last_status = TASK_STATUS_RUNNING
        task.last_message = "执行中"
        run_id: int | None = None
        try:
            run_id = self.datahub.cache.start_task_run(task.name)
            message = await task.handler()
            task.last_status = TASK_STATUS_SUCCESS
            task.last_message = message
            self.datahub.cache.finish_task_run(run_id, TASK_STATUS_SUCCESS, message)
            return message
        except asyncio.CancelledError:
            message = f"{task.display_name} 已取消"
            task.last_status = TASK_STATUS_CANCELLED
            task.last_message = message
            _record_task_end(self.datahub.cache, run_id, TASK_STATUS_CANCELLED, message)
            raise
        except Exception as exc:
            message = _task_error_message(exc)
            task.last_status = TASK_STATUS_FAILED
            task.last_message = message
            _record_task_end(self.datahub.cache, run_id, TASK_STATUS_FAILED, message)
            if manual:
                raise RuntimeError(message) from exc
            return message
        finally:
            task.running = False
            finished_at = datetime.now()
            task.last_finished_at = finished_at
            _reschedule_task(task, manual, finished_at)

    async def _refresh_watch_quotes(self) -> str:
        symbols, skipped_count = _scheduler_symbols(
            self.datahub.cache.watchlist_symbols(),
            self.settings.seed_symbols,
        )
        _save_symbol_skip_event(self.datahub.cache, "quote", "观察池报价刷新", skipped_count)
        if not symbols:
            message = "无有效观察个股，已跳过报价刷新"
            self.datahub.cache.save_monitor_event("warning", "quote", message)
            return message
        quotes = await self.datahub.quotes(symbols, use_cache=False)
        summary = _quote_refresh_summary(symbols, quotes)
        message = _quote_refresh_message(summary)
        level = "warning" if summary.fallback_symbols or summary.missing_symbols else "info"
        self.datahub.cache.save_monitor_event(level, "quote", message)
        if summary.returned == 0:
            raise RuntimeError(message)
        return message

    async def _refresh_key_klines(self) -> str:
        symbols, skipped_count = _scheduler_symbols(
            self.datahub.cache.watchlist_symbols(),
            self.settings.seed_symbols,
            limit=self.settings.scheduler_kline_symbols_limit,
        )
        _save_symbol_skip_event(self.datahub.cache, "kline", "关键个股K线刷新", skipped_count)
        if not symbols:
            message = "无有效关键个股，已跳过日K线刷新"
            self.datahub.cache.save_monitor_event("warning", "kline", message)
            return message
        summary = await self._refresh_key_kline_symbols(symbols)
        self._save_kline_refresh_failure_event(summary.failures)
        if summary.failures and summary.refreshed == 0:
            raise RuntimeError(f"关键个股日K线全部刷新失败：{_kline_failure_detail(summary.failures)}")
        message = _kline_refresh_message(summary)
        level = "warning" if summary.failures or summary.fallback_cache else "info"
        self.datahub.cache.save_monitor_event(level, "kline", message)
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

    def _save_kline_refresh_failure_event(self, failures: tuple[str, ...]) -> None:
        if failures:
            self.datahub.cache.save_monitor_event(
                "warning",
                "kline",
                f"关键个股K线刷新失败 {len(failures)} 只：{_kline_failure_detail(failures)}",
            )

    async def _refresh_plate_rank(self) -> str:
        rows = await self.datahub.plate_rank(limit=20, refresh=True)
        message = f"已刷新 {len(rows)} 条行业背景数据"
        self.datahub.cache.save_monitor_event("info", "plate", message)
        return message

    async def _check_data_health(self) -> str:
        stats = self.datahub.cache.stats()
        capability_rows = self.datahub.cache.provider_capability_statuses()
        provider_rows = self.datahub.cache.provider_statuses()
        health_events = _data_health_events(stats, capability_rows, provider_rows, self.settings)
        for event in health_events:
            self.datahub.cache.save_monitor_event(event.level, event.category, event.message)
        events = [event.message for event in health_events]

        removed = self.datahub.cache.cleanup_runtime_rows()
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
        self.datahub.cache.save_monitor_event(level, "alert", message)
        if summary.checked_count and summary.failed_count == summary.checked_count:
            raise RuntimeError(message)
        return message


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


def _reschedule_task(task: LocalTask, manual: bool, finished_at: datetime) -> None:
    interval_seconds = _positive_int_at_least(task.interval_seconds, 1)
    task.next_run_at = finished_at + timedelta(seconds=interval_seconds)


def _scheduler_symbols(
    watchlist_symbols: Iterable[object] | None,
    seed_symbols: Iterable[object] | None,
    *,
    limit: int | None = None,
) -> tuple[list[str], int]:
    watchlist = list(watchlist_symbols or [])
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


def _seconds_since(value: str | None) -> float | None:
    return non_negative_seconds_since_text(value)


def _data_health_events(
    stats: CacheStats,
    capability_rows: list[ProviderCapabilityStatus],
    provider_rows: list[ProviderStatus],
    settings,
) -> list[HealthEvent]:
    events = [
        *_provider_health_events(capability_rows, provider_rows),
        *_cache_freshness_events(stats, settings),
    ]
    if events:
        return events
    return [HealthEvent("info", "health", "报价、K线、行业背景和数据源状态正常")]


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


def _cache_freshness_events(stats: CacheStats, settings) -> list[HealthEvent]:
    events: list[HealthEvent] = []
    for rule in _CACHE_FRESHNESS_RULES:
        events.extend(
            _cache_event_for_rule(getattr(stats, rule.timestamp_attr), getattr(settings, rule.threshold_attr), rule)
        )
    return events


def _quote_cache_events(latest_quote_at: str | None, stale_warning_seconds: int) -> list[HealthEvent]:
    return _cache_event_for_rule(latest_quote_at, stale_warning_seconds, _QUOTE_CACHE_RULE)


def _kline_cache_events(latest_kline_at: str | None, cache_seconds: int) -> list[HealthEvent]:
    return _cache_event_for_rule(latest_kline_at, cache_seconds, _KLINE_CACHE_RULE)


def _cache_event_for_rule(
    latest_at: str | None,
    threshold_seconds: int,
    rule: CacheFreshnessRule,
) -> list[HealthEvent]:
    delay = _seconds_since(latest_at)
    if delay is None:
        message = rule.invalid_message if latest_at else rule.missing_message
        return [HealthEvent("warning", rule.category, message)]
    threshold = _positive_int_at_least(threshold_seconds, 1)
    if delay > threshold * rule.threshold_multiplier:
        return [HealthEvent("warning", rule.category, rule.stale_message(int(delay)))]
    return []


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


def _task_error_message(exc: Exception) -> str:
    text = " ".join(str(exc).strip().split())
    if not text:
        return exc.__class__.__name__
    return text[:TASK_ERROR_MAX_LENGTH]


def _record_task_end(cache, run_id: int | None, status: str, message: str) -> None:
    if run_id is not None:
        try:
            cache.finish_task_run(run_id, status, message)
        except Exception:
            pass
    try:
        cache.save_monitor_event("warning", "task", message, symbol=None)
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
