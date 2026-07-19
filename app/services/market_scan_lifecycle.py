from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from app.services.datahub_runtime import run_cache_io
from app.services.instance_guard import FileInstanceGuard, InstanceGuard


RunExecutor = Callable[[int, asyncio.Event], Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class MarketScanStopSnapshot:
    run_ids: tuple[int, ...]
    tasks: tuple[asyncio.Task[None], ...]


class MarketScanLifecycle:
    """Own process leadership and local background-task bookkeeping."""

    def __init__(self, cache: object, *, instance_guard: InstanceGuard | None = None) -> None:
        self._cache = cache
        self._instance_guard = instance_guard if instance_guard is not None else default_market_scan_guard(cache)
        self.lock = asyncio.Lock()
        self._guard_acquired = False
        self._ownership_reconciled = False
        self._started = False
        self._stopping = False
        self._closed = False
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._cancel_events: dict[int, asyncio.Event] = {}
        self._quiescent_event = asyncio.Event()
        self._quiescent_event.set()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active_run_ids(self) -> tuple[int, ...]:
        return tuple(self._tasks)

    @property
    def is_quiescent(self) -> bool:
        return self._quiescent_event.is_set()

    async def wait_until_quiescent(self) -> None:
        if self.is_quiescent:
            return
        await self._quiescent_event.wait()

    async def start(self) -> int:
        async with self.lock:
            self.require_open()
            if self._stopping:
                raise RuntimeError("全市场扫描管理器正在停止")
            if self._started:
                return 0
            acquired, reconciled = await self.ensure_instance_guard()
            if not acquired:
                return 0
            self._started = True
            self._quiescent_event.clear()
            return reconciled

    async def begin_stop(self, *, close: bool) -> MarketScanStopSnapshot | None:
        async with self.lock:
            if close:
                self._closed = True
            if self._stopping:
                return None
            if not self._started and not self._guard_acquired and not self._tasks:
                self._quiescent_event.set()
                return None
            self._stopping = True
            self._quiescent_event.clear()
            run_ids = tuple(self._tasks)
            tasks = tuple(task for task in self._tasks.values() if not task.done())
            for event in self._cancel_events.values():
                event.set()
            for task in tasks:
                task.cancel()
            return MarketScanStopSnapshot(run_ids=run_ids, tasks=tasks)

    async def finish_stop(self) -> None:
        async with self.lock:
            try:
                self._started = False
                await self.release_instance_guard()
            finally:
                self._stopping = False
                self._mark_quiescent_if_idle()

    def launch(self, run_id: int, executor: RunExecutor) -> None:
        cancel_event = asyncio.Event()
        task = asyncio.create_task(executor(run_id, cancel_event), name=f"market-scan-{run_id}")
        self._cancel_events[run_id] = cancel_event
        self._tasks[run_id] = task
        task.add_done_callback(partial(self._task_done, run_id))

    def cancel_local(self, run_id: int) -> asyncio.Task[None] | None:
        event = self._cancel_events.get(run_id)
        if event is not None:
            event.set()
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        return task

    async def ensure_instance_guard(self) -> tuple[bool, int]:
        if self._guard_acquired:
            return True, await self._reconcile_ownership()
        acquire = asyncio.create_task(
            run_cache_io(self._instance_guard.acquire),
            name="market-scan-instance-guard-acquire",
        )
        try:
            acquired = await asyncio.shield(acquire)
        except asyncio.CancelledError:
            acquired = await asyncio.shield(acquire)
            if acquired:
                await run_cache_io(self._instance_guard.release)
            raise
        self._guard_acquired = acquired
        if not acquired:
            return False, 0
        try:
            return True, await self._reconcile_ownership()
        except BaseException:
            await self.release_instance_guard()
            raise

    async def require_instance_guard(self, busy_message: str) -> None:
        acquired, _reconciled = await self.ensure_instance_guard()
        if not acquired:
            raise RuntimeError(busy_message)

    async def release_instance_guard(self) -> None:
        if not self._guard_acquired:
            return
        ownership_reconciled = self._ownership_reconciled
        self._guard_acquired = False
        self._ownership_reconciled = False
        release = asyncio.create_task(
            run_cache_io(self._instance_guard.release),
            name="market-scan-instance-guard-release",
        )
        try:
            await asyncio.shield(release)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(release)
            except BaseException:
                self._guard_acquired = True
                self._ownership_reconciled = ownership_reconciled
                raise
            raise
        except BaseException:
            self._guard_acquired = True
            self._ownership_reconciled = ownership_reconciled
            raise

    def require_open(self) -> None:
        if self._closed:
            raise RuntimeError("全市场扫描管理器已关闭")

    async def _reconcile_ownership(self) -> int:
        if self._ownership_reconciled:
            return 0
        reconciled = await run_cache_io(getattr(self._cache, "reconcile_incomplete_market_scans"))
        self._ownership_reconciled = True
        return reconciled

    def _task_done(self, run_id: int, task: asyncio.Task[None]) -> None:
        self._tasks.pop(run_id, None)
        self._cancel_events.pop(run_id, None)
        self._mark_quiescent_if_idle()
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _mark_quiescent_if_idle(self) -> None:
        if not self._started and not self._stopping and not self._guard_acquired and not self._tasks:
            self._quiescent_event.set()


def default_market_scan_guard(cache: object) -> InstanceGuard:
    cache_path = getattr(cache, "path", None)
    if cache_path is None:
        raise ValueError("全市场扫描需要可定位的 SQLite 缓存路径")
    return FileInstanceGuard(Path(f"{cache_path}.market-scan.lock"))


__all__ = ["MarketScanLifecycle", "MarketScanStopSnapshot", "default_market_scan_guard"]
