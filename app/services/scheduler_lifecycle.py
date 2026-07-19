from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Iterable

from app.services.scheduler_contracts import SchedulerRuntimeContext
from app.services.scheduler_helpers import (
    _consume_future_exception,
    _offload,
    _wait_for_tasks_bounded,
)
from app.services.scheduler_schedule import _positive_float_or_default
from app.utils.fallback_logging import report_persistence_failure


class SchedulerLifecycleMixin(SchedulerRuntimeContext):
    @property
    def is_quiescent(self) -> bool:
        """Whether no scheduler work can outlive release of its instance guard."""

        return (
            self._runner is None
            and not self._active_tasks
            and not self._shutdown_tasks
            and self._guard_release_task is None
            and not self._guard_acquired
            and not self._scheduler_guard_active
            and self._manual_guard_users == 0
        )

    async def wait_until_quiescent(self) -> None:
        if self.is_quiescent:
            return
        await self._quiescent_event.wait()

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
            self._quiescent_event.clear()
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
            self._mark_quiescent_if_idle()

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
                self._mark_quiescent_if_idle()
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
                    self._mark_quiescent_if_idle()
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
        if not self._guard_acquired or self._scheduler_guard_active or self._manual_guard_users or self._shutdown_tasks:
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

    def _mark_quiescent_if_idle(self) -> None:
        if self.is_quiescent:
            self._quiescent_event.set()

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
            self._mark_quiescent_if_idle()

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
        except Exception as exc:
            report_persistence_failure("scheduler monitor persistence failed", exc)
