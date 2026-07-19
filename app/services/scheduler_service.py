from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Awaitable, Callable

from app.services.datahub import DataHub
from app.services.market_scan_manager import MarketScanManager
from app.services.scheduler_contracts import SchedulerInstanceGuard, _TASK_DEFINITIONS
from app.services.scheduler_execution import SchedulerExecutionMixin
from app.services.scheduler_helpers import _default_instance_guard
from app.services.scheduler_lifecycle import SchedulerLifecycleMixin
from app.services.scheduler_schedule import _build_local_tasks
from app.services.scheduler_tasks import SchedulerTaskHandlersMixin


class LocalDataScheduler(
    SchedulerLifecycleMixin,
    SchedulerExecutionMixin,
    SchedulerTaskHandlersMixin,
):
    def __init__(
        self,
        datahub: DataHub,
        *,
        instance_guard: SchedulerInstanceGuard | None = None,
        market_scanner: MarketScanManager | None = None,
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
        self.market_scanner = market_scanner
        self._guard_acquired = False
        self._scheduler_guard_active = False
        self._standby = False
        self._manual_guard_users = 0
        self._shutdown_tasks: set[asyncio.Task] = set()
        self._guard_release_task: asyncio.Task[None] | None = None
        self._quiescent_event = asyncio.Event()
        self._quiescent_event.set()
        self.tasks = _build_local_tasks(self.settings, datetime.now(), self._task_handlers())

    def bind_instance_guard(self, instance_guard: SchedulerInstanceGuard) -> None:
        if self._guard_acquired or self._runner is not None or self._active_tasks:
            raise RuntimeError("运行中的调度器不能更换实例锁")
        self._instance_guard = instance_guard

    def set_runtime_standby(self, standby: bool) -> None:
        """Reflect coordinator-owned standby state in the public status view."""

        self._standby = bool(standby)

    def _task_handlers(self) -> dict[str, Callable[[], Awaitable[str]]]:
        handlers: dict[str, Callable[[], Awaitable[str]]] = {}
        for definition in _TASK_DEFINITIONS:
            handlers[definition.name] = getattr(self, definition.handler_name)
        return handlers
