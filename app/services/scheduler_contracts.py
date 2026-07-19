from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable, Protocol

from app.services.instance_guard import FileInstanceGuard
from app.utils.time import datetime_to_text


if TYPE_CHECKING:
    from app.config import Settings
    from app.services.datahub import DataHub
    from app.services.market_scan_manager import MarketScanManager


TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_DEGRADED = "degraded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"
TASK_ERROR_MAX_LENGTH = 120
KLINE_FAILURE_DETAIL_LIMIT = 3
PROVIDER_FAILURE_DETAIL_LIMIT = 5
INSTANCE_GUARD_BUSY_MESSAGE = "已有其他进程运行本地数据调度器，手动任务未执行"


class SchedulerInstanceGuard(Protocol):
    def acquire(self) -> bool: ...

    def release(self) -> None: ...


class NoopSchedulerInstanceGuard:
    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None

    def held_by_other(self) -> bool:
        return False


class FileSchedulerInstanceGuard(FileInstanceGuard):
    """Backward-compatible scheduler name for the shared file guard."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)


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

    status: str

    def __new__(cls, message: str, status: str = TASK_STATUS_SUCCESS) -> TaskExecutionResult:
        result = super().__new__(cls, message)
        result.status = status
        return result


if TYPE_CHECKING:

    class SchedulerRuntimeContext:
        """Host contract shared by scheduler mixins during static analysis."""

        datahub: DataHub
        settings: Settings
        enabled: bool
        started_at: datetime | None
        tasks: dict[str, LocalTask]
        market_scanner: MarketScanManager | None
        _stop_event: asyncio.Event
        _runner: asyncio.Task[None] | None
        _active_tasks: set[asyncio.Task[str]]
        _lifecycle_lock: asyncio.Lock
        _manual_run_lock: asyncio.Lock
        _instance_guard: SchedulerInstanceGuard
        _guard_acquired: bool
        _scheduler_guard_active: bool
        _standby: bool
        _manual_guard_users: int
        _shutdown_tasks: set[asyncio.Task]
        _guard_release_task: asyncio.Task[None] | None
        _quiescent_event: asyncio.Event

        async def _loop(self) -> None: ...

        async def _acquire_instance_guard(self) -> bool: ...

        async def _release_instance_guard(self) -> None: ...

        def _mark_quiescent_if_idle(self) -> None: ...

        async def _wait_for_shutdown_tasks(
            self,
            tasks: Iterable[asyncio.Task[object]],
            *,
            cancel_first: bool = False,
        ) -> set[asyncio.Task]: ...

        async def _save_monitor_event(
            self,
            level: str,
            category: str,
            message: str,
            *,
            symbol: str | None = None,
        ) -> None: ...

else:

    class SchedulerRuntimeContext:
        """Runtime-empty base preserving cooperative scheduler mixin lookup."""


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
