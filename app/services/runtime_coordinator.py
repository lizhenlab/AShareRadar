from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import threading

from app.services.instance_guard import FileInstanceGuard, InstanceGuard
from app.services.provider_errors import sanitize_provider_error


RUNTIME_LEADER_LOCK_SUFFIX = ".runtime-leader.lock"
DEFAULT_TAKEOVER_POLL_SECONDS = 0.5


class RuntimeLeadershipGuard:
    """Service view of the single runtime leadership lease."""

    def __init__(self, leadership: RuntimeLeadership) -> None:
        self._leadership = leadership

    def acquire(self) -> bool:
        return self._leadership.is_leader

    def release(self) -> None:
        return None

    def held_by_other(self) -> bool:
        return self._leadership.held_by_other()


class RuntimeLeadership:
    def __init__(self, guard: InstanceGuard) -> None:
        self._guard = guard
        self._state_lock = threading.Lock()
        self._is_leader = False

    @classmethod
    def for_cache_path(cls, cache_path: Path) -> RuntimeLeadership:
        return cls(FileInstanceGuard(Path(f"{cache_path}{RUNTIME_LEADER_LOCK_SUFFIX}")))

    @property
    def is_leader(self) -> bool:
        with self._state_lock:
            return self._is_leader

    def service_guard(self) -> RuntimeLeadershipGuard:
        return RuntimeLeadershipGuard(self)

    def try_acquire(self) -> bool:
        with self._state_lock:
            if self._is_leader:
                return True
            acquired = self._guard.acquire()
            if acquired:
                self._is_leader = True
            return acquired

    def release(self) -> None:
        with self._state_lock:
            if not self._is_leader:
                return
            self._is_leader = False
            self._guard.release()

    def held_by_other(self) -> bool:
        with self._state_lock:
            if self._is_leader:
                return False
            probe = getattr(self._guard, "held_by_other", None)
            return bool(probe()) if callable(probe) else True


class RuntimeCoordinator:
    """Atomically owns and activates the scheduler plus full-market scanner."""

    def __init__(
        self,
        leadership: RuntimeLeadership,
        scheduler,
        market_scanner,
        *,
        takeover_poll_seconds: float = DEFAULT_TAKEOVER_POLL_SECONDS,
    ) -> None:
        self.leadership = leadership
        self.scheduler = scheduler
        self.market_scanner = market_scanner
        self.takeover_poll_seconds = max(0.05, float(takeover_poll_seconds))
        self._lifecycle_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._lease_release_task: asyncio.Task[None] | None = None
        self._active = False

    async def start(self) -> bool:
        async with self._lifecycle_lock:
            if self._runner is not None and not self._runner.done():
                return self.leadership.is_leader
            if self._runner is not None:
                await asyncio.gather(self._runner, return_exceptions=True)
            self._stop_event.clear()
            if self._active and self.leadership.is_leader:
                active = True
            elif self.leadership.is_leader or (
                self._lease_release_task is not None and not self._lease_release_task.done()
            ):
                active = False
            else:
                active = await self._try_activate()
            self._runner = asyncio.create_task(self._standby_loop(), name="runtime-leadership-standby")
            self._runner.add_done_callback(_consume_future_exception)
            return active

    async def stop(self) -> None:
        self._stop_event.set()
        self._set_scheduler_standby(False)
        runner = self._runner
        if runner is not None and runner is not asyncio.current_task():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        async with self._lifecycle_lock:
            self._runner = None
            await self._deactivate()

    async def _standby_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.takeover_poll_seconds)
                continue
            except TimeoutError:
                pass
            if self.leadership.is_leader:
                continue
            async with self._lifecycle_lock:
                if self._stop_event.is_set() or self.leadership.is_leader:
                    continue
                try:
                    await self._try_activate()
                except Exception as exc:
                    await self._report_activation_failure(exc)

    async def _try_activate(self) -> bool:
        acquired = await asyncio.to_thread(self.leadership.try_acquire)
        if not acquired:
            self._set_scheduler_standby(True)
            return False
        self._set_scheduler_standby(False)
        try:
            if self.market_scanner is not None:
                await self.market_scanner.start()
            await self.scheduler.start()
        except BaseException:
            self._active = False
            try:
                await self._stop_services(final=False)
            finally:
                await self._release_or_defer_leadership()
            raise
        self._active = True
        return True

    async def _deactivate(self) -> None:
        self._active = False
        if not self.leadership.is_leader:
            self._set_scheduler_standby(False)
            return
        try:
            await self._stop_services(final=True)
        finally:
            await self._release_or_defer_leadership()

    async def _stop_services(self, *, final: bool) -> None:
        try:
            await self.scheduler.stop()
        finally:
            if self.market_scanner is not None:
                rollback = getattr(self.market_scanner, "rollback_activation", None)
                if not final and callable(rollback):
                    await rollback()
                else:
                    await self.market_scanner.stop()

    async def _release_or_defer_leadership(self) -> None:
        release_task = self._lease_release_task
        if release_task is not None and not release_task.done():
            return
        if self._services_are_quiescent():
            await self._release_leadership()
            return
        release_task = asyncio.create_task(
            self._release_leadership_after_quiescence(),
            name="runtime-leadership-deferred-release",
        )
        self._lease_release_task = release_task
        release_task.add_done_callback(_consume_future_exception)

    async def _release_leadership_after_quiescence(self) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.gather(
                *(self._wait_for_service_quiescence(service) for service in self._runtime_services())
            )
            async with self._lifecycle_lock:
                if not self._services_are_quiescent():
                    raise RuntimeError("运行时服务未真正静止，拒绝释放领导权")
                await self._release_leadership()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._report_activation_failure(exc)
        finally:
            if self._lease_release_task is current_task:
                self._lease_release_task = None

    async def _release_leadership(self) -> None:
        await asyncio.to_thread(self.leadership.release)
        self._set_scheduler_standby(not self._stop_event.is_set())

    def _runtime_services(self) -> tuple[object, ...]:
        if self.market_scanner is None:
            return (self.scheduler,)
        return self.scheduler, self.market_scanner

    def _services_are_quiescent(self) -> bool:
        return all(_service_is_quiescent(service) for service in self._runtime_services())

    @staticmethod
    async def _wait_for_service_quiescence(service: object) -> None:
        waiter = getattr(service, "wait_until_quiescent", None)
        if callable(waiter):
            await waiter()
            return
        if not _service_is_quiescent(service):
            raise RuntimeError(f"{type(service).__name__} 未提供静止等待契约")

    def _set_scheduler_standby(self, standby: bool) -> None:
        setter = getattr(self.scheduler, "set_runtime_standby", None)
        if callable(setter):
            setter(standby)

    async def _report_activation_failure(self, exc: Exception) -> None:
        detail = " ".join(sanitize_provider_error(exc).split())[:300] or "未知错误"
        message = f"运行时领导权接管失败：{type(exc).__name__}: {detail}"
        cache = getattr(getattr(self.scheduler, "datahub", None), "cache", None)
        save_event = getattr(cache, "save_monitor_event", None)
        if callable(save_event):
            try:
                await asyncio.to_thread(save_event, "error", "runtime-leadership", message)
                return
            except Exception:
                pass
        print(message, file=sys.stderr)


def _consume_future_exception(future: asyncio.Future) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except asyncio.CancelledError:
        pass


def _service_is_quiescent(service: object) -> bool:
    marker = getattr(service, "is_quiescent", True)
    return bool(marker() if callable(marker) else marker)


__all__ = [
    "DEFAULT_TAKEOVER_POLL_SECONDS",
    "RUNTIME_LEADER_LOCK_SUFFIX",
    "RuntimeCoordinator",
    "RuntimeLeadership",
    "RuntimeLeadershipGuard",
]
