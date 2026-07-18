from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable, Iterable, Iterator, Mapping
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from functools import partial
import threading
import time
from typing import Any, Generic, ParamSpec, TypeVar

from app.services.datahub_status import _provider_error_text
from app.services.provider_errors import (
    ProviderCoverageMiss,
    is_provider_coverage_miss,
    sanitize_provider_error,
)


T = TypeVar("T")
P = ParamSpec("P")

__all__ = [
    "ProviderAttempt",
    "ProviderCallBusyError",
    "ProviderCallTimeoutError",
    "ProviderCoverageMiss",
    "ProviderRuntime",
    "TimedProviderCall",
    "provider_source_name",
    "run_cache_io",
    "run_cache_io_best_effort",
    "run_provider_io",
]

PROVIDER_IO_MAX_WORKERS = 4
PROVIDER_CAPABILITY_MAX_IN_FLIGHT = 2
PROVIDER_SHUTDOWN_TIMEOUT_SECONDS = 1.0
_PROVIDER_IO_EXECUTOR: ContextVar[ThreadPoolExecutor | None] = ContextVar(
    "ashare_radar_provider_io_executor",
    default=None,
)
_PROVIDER_IO_TRACKER: ContextVar[Callable[[ConcurrentFuture[Any]], None] | None] = ContextVar(
    "ashare_radar_provider_io_tracker",
    default=None,
)


async def run_cache_io(call: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    return await asyncio.to_thread(call, *args, **kwargs)


async def run_cache_io_best_effort(
    call: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T | None:
    try:
        return await run_cache_io(call, *args, **kwargs)
    except Exception:
        return None


async def run_provider_io(call: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    loop = asyncio.get_running_loop()
    context = copy_context()
    bound = partial(call, *args, **kwargs)
    executor = _PROVIDER_IO_EXECUTOR.get()
    if executor is None:
        return await loop.run_in_executor(None, context.run, bound)

    worker = executor.submit(context.run, bound)
    tracker = _PROVIDER_IO_TRACKER.get()
    if tracker is not None:
        tracker(worker)
    return await asyncio.wrap_future(worker)


@dataclass(frozen=True)
class ProviderAttempt:
    index: int
    name: str
    provider: object


@dataclass(frozen=True)
class TimedProviderCall(Generic[T]):
    value: T
    latency_ms: float


@dataclass
class _ProviderCallState:
    task: asyncio.Future[Any]
    waiters: int = 0
    orphaned: bool = False


class ProviderCallBusyError(RuntimeError):
    """Provider admission is full or an orphaned call is still running."""


class ProviderCallTimeoutError(TimeoutError):
    """The caller timed out while the tracked provider task continues safely."""


class ProviderRuntime:
    def __init__(self, cache, settings) -> None:
        self.cache = cache
        self.settings = settings
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._provider_calls: dict[
            tuple[str, str, Hashable],
            _ProviderCallState,
        ] = {}
        self._provider_conditions: dict[tuple[str, str], asyncio.Condition] = {}
        self._provider_workers: dict[
            tuple[str, str],
            set[ConcurrentFuture[Any]],
        ] = {}
        self._worker_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=PROVIDER_IO_MAX_WORKERS,
            thread_name_prefix="ashare-provider",
        )
        self._executor_shutdown = False
        self._closed = False
        self._quiesced = False

    async def call_provider(
        self,
        name: str,
        kind: str,
        start: Callable[[], Awaitable[T]],
        *,
        request_key: Hashable | None = None,
    ) -> T:
        if self._closed:
            raise RuntimeError("ProviderRuntime 已关闭")
        identity = request_key if request_key is not None else object()
        try:
            hash(identity)
        except TypeError as exc:
            raise TypeError("provider request_key 必须可哈希") from exc
        capability_key = (name, kind)
        full_key = (name, kind, identity)
        timeout = max(0.0, float(self.settings.provider_call_timeout_seconds))
        deadline = asyncio.get_running_loop().time() + timeout
        state = await self._admit_provider_call(
            capability_key,
            full_key,
            start,
            deadline=deadline,
        )
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        return await self._wait_for_provider_task(
            capability_key,
            full_key,
            state,
            f"{name} {kind} 调用",
            timeout=remaining,
        )

    async def aclose(self, timeout: float = PROVIDER_SHUTDOWN_TIMEOUT_SECONDS) -> bool:
        if self._quiesced:
            return True
        self._closed = True
        tasks = self._active_provider_tasks()
        for task in tasks:
            task.cancel()

        if not self._executor_shutdown:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor_shutdown = True

        await self._wait_for_quiescence(tasks, timeout=max(0.0, timeout))
        if self._active_provider_tasks() or self._active_provider_workers():
            return False

        self._provider_calls.clear()
        with self._worker_lock:
            self._provider_workers.clear()
        self._cooldowns.clear()
        self._quiesced = True
        return True

    async def _wait_for_quiescence(
        self,
        tasks: list[asyncio.Future[Any]],
        *,
        timeout: float,
    ) -> None:
        waiters = set(tasks)
        waiters.update(asyncio.wrap_future(worker) for worker in self._active_provider_workers())
        if waiters:
            await asyncio.wait(waiters, timeout=timeout)

    async def _admit_provider_call(
        self,
        capability_key: tuple[str, str],
        full_key: tuple[str, str, Hashable],
        start: Callable[[], Awaitable[T]],
        *,
        deadline: float,
    ) -> _ProviderCallState:
        condition = self._provider_conditions.setdefault(capability_key, asyncio.Condition())
        async with condition:
            while True:
                if self._closed:
                    raise RuntimeError("ProviderRuntime 已关闭")
                existing = self._provider_calls.get(full_key)
                if existing is not None and not existing.task.done():
                    existing.waiters += 1
                    existing.orphaned = False
                    return existing
                if existing is not None:
                    self._provider_calls.pop(full_key, None)
                if self._provider_has_orphaned_call(capability_key):
                    raise ProviderCallBusyError(f"{capability_key[0]} {capability_key[1]} 上一次调用仍在后台执行")
                if self._active_provider_call_count(capability_key) < PROVIDER_CAPABILITY_MAX_IN_FLIGHT:
                    state = self._start_provider_call(capability_key, full_key, start)
                    state.waiters = 1
                    return state

                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ProviderCallBusyError(f"{capability_key[0]} {capability_key[1]} 当前并发请求较多")
                try:
                    await asyncio.wait_for(condition.wait(), timeout=remaining)
                except TimeoutError as exc:
                    raise ProviderCallBusyError(f"{capability_key[0]} {capability_key[1]} 当前并发请求较多") from exc

    def _start_provider_call(
        self,
        capability_key: tuple[str, str],
        full_key: tuple[str, str, Hashable],
        start: Callable[[], Awaitable[T]],
    ) -> _ProviderCallState:
        executor_token = _PROVIDER_IO_EXECUTOR.set(self._executor)
        tracker_token = _PROVIDER_IO_TRACKER.set(partial(self._track_provider_worker, capability_key))
        try:
            task = asyncio.ensure_future(start())
        finally:
            _PROVIDER_IO_TRACKER.reset(tracker_token)
            _PROVIDER_IO_EXECUTOR.reset(executor_token)
        state = _ProviderCallState(task=task)
        self._provider_calls[full_key] = state
        task.add_done_callback(partial(self._finish_provider_call, full_key, state))
        return state

    async def _wait_for_provider_task(
        self,
        capability_key: tuple[str, str],
        full_key: tuple[str, str, Hashable],
        state: _ProviderCallState,
        label: str,
        *,
        timeout: float,
    ) -> T:
        task = state.task

        # Cancelling an executor waiter cannot stop its worker thread. asyncio.wait
        # leaves the shared provider task alive until the real SDK call exits.
        try:
            done, _pending = await asyncio.wait(
                {task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task not in done:
                raise ProviderCallTimeoutError(f"{label}超过 {self.settings.provider_call_timeout_seconds:g} 秒，后台任务仍在收尾")
            return task.result()
        finally:
            condition = self._provider_conditions[capability_key]
            async with condition:
                state.waiters = max(0, state.waiters - 1)
                if state.waiters == 0 and not task.done():
                    state.orphaned = True
                if task.done() and self._provider_calls.get(full_key) is state:
                    self._provider_calls.pop(full_key, None)
                condition.notify_all()

    async def timed_provider_call(
        self,
        name: str,
        kind: str,
        start: Callable[[], Awaitable[T]],
        *,
        request_key: Hashable | None = None,
    ) -> TimedProviderCall[T]:
        started = time.perf_counter()
        value = await self.call_provider(name, kind, start, request_key=request_key)
        return TimedProviderCall(value=value, latency_ms=round((time.perf_counter() - started) * 1000, 2))

    def attempts(
        self,
        priority_rows: Iterable[tuple[int, str]],
        providers: Mapping[str, object],
        kind: str,
        errors: list[str],
    ) -> Iterator[ProviderAttempt]:
        for index, name in priority_rows:
            if self.is_cooling(name, kind):
                errors.append(f"{name}: 最近失败，短暂冷却中")
                continue
            if self._provider_has_orphaned_call((name, kind)):
                errors.append(f"{name}: 上一次调用仍在后台执行")
                continue
            provider = providers.get(name)
            if provider is None:
                errors.append(f"{name}: 数据源未注册")
                continue
            yield ProviderAttempt(index=index, name=name, provider=provider)

    def is_cooling(self, name: str, kind: str = "general") -> bool:
        until = self._cooldowns.get((name, kind))
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        self._cooldowns.pop((name, kind), None)
        return False

    def provider_call_in_flight(self, name: str, kind: str) -> bool:
        key = (name, kind)
        if self._active_provider_workers(key):
            return True
        return self._active_provider_call_count(key) > 0

    def _active_provider_call_count(self, key: tuple[str, str]) -> int:
        return sum(not state.task.done() for (name, kind, _request_key), state in self._provider_calls.items() if (name, kind) == key)

    def _provider_has_orphaned_call(self, key: tuple[str, str]) -> bool:
        return any(state.orphaned and not state.task.done() for (name, kind, _request_key), state in self._provider_calls.items() if (name, kind) == key)

    def _finish_provider_call(
        self,
        full_key: tuple[str, str, Hashable],
        state: _ProviderCallState,
        task: asyncio.Future[Any],
    ) -> None:
        if self._provider_calls.get(full_key) is state:
            self._provider_calls.pop(full_key, None)
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    def _track_provider_worker(
        self,
        key: tuple[str, str],
        worker: ConcurrentFuture[Any],
    ) -> None:
        with self._worker_lock:
            self._provider_workers.setdefault(key, set()).add(worker)
        worker.add_done_callback(partial(self._finish_provider_worker, key))

    def _finish_provider_worker(
        self,
        key: tuple[str, str],
        worker: ConcurrentFuture[Any],
    ) -> None:
        with self._worker_lock:
            workers = self._provider_workers.get(key)
            if workers is None:
                return
            workers.discard(worker)
            if not workers:
                self._provider_workers.pop(key, None)

    def _active_provider_tasks(self) -> list[asyncio.Future[Any]]:
        return [state.task for state in self._provider_calls.values() if not state.task.done()]

    def _active_provider_workers(
        self,
        key: tuple[str, str] | None = None,
    ) -> list[ConcurrentFuture[Any]]:
        with self._worker_lock:
            if key is not None:
                return [worker for worker in self._provider_workers.get(key, ()) if not worker.done()]
            return [worker for workers in self._provider_workers.values() for worker in workers if not worker.done()]

    def record_success(self, name: str, index: int, latency_ms: float, kind: str) -> None:
        try:
            self.cache.update_provider_capability_success(name, kind, index, latency_ms)
        except Exception:
            pass
        self.clear_cooldown(name, kind)

    def record_attempt_success(self, attempt: ProviderAttempt, kind: str, latency_ms: float) -> None:
        self.record_success(attempt.name, attempt.index, latency_ms, kind)

    async def record_success_async(self, name: str, index: int, latency_ms: float, kind: str) -> None:
        await run_cache_io_best_effort(
            self.cache.update_provider_capability_success,
            name,
            kind,
            index,
            latency_ms,
        )
        self.clear_cooldown(name, kind)

    async def record_attempt_success_async(
        self,
        attempt: ProviderAttempt,
        kind: str,
        latency_ms: float,
    ) -> None:
        await self.record_success_async(attempt.name, attempt.index, latency_ms, kind)

    def record_failure(self, name: str, index: int, exc: Exception, kind: str) -> None:
        if is_provider_coverage_miss(exc) or isinstance(exc, ProviderCallBusyError):
            return
        error_text = self._sanitized_error_text(exc)
        try:
            self.cache.update_provider_capability_failure(name, kind, index, error_text)
        except Exception:
            pass
        cooldown_seconds = max(0, self.settings.provider_failure_cooldown_seconds)
        if cooldown_seconds:
            self._cooldowns[(name, kind)] = time.monotonic() + cooldown_seconds

    async def record_failure_async(self, name: str, index: int, exc: Exception, kind: str) -> None:
        if is_provider_coverage_miss(exc) or isinstance(exc, ProviderCallBusyError):
            return
        error_text = self._sanitized_error_text(exc)
        await run_cache_io_best_effort(
            self.cache.update_provider_capability_failure,
            name,
            kind,
            index,
            error_text,
        )
        cooldown_seconds = max(0, self.settings.provider_failure_cooldown_seconds)
        if cooldown_seconds:
            self._cooldowns[(name, kind)] = time.monotonic() + cooldown_seconds

    def record_attempt_failure(
        self,
        attempt: ProviderAttempt,
        kind: str,
        exc: Exception,
        errors: list[str] | None = None,
        record_failure=None,
    ) -> None:
        error_text = self._sanitized_error_text(exc)
        if errors is not None:
            errors.append(f"{attempt.name}: {error_text}")
        if is_provider_coverage_miss(exc) or isinstance(exc, ProviderCallBusyError):
            return
        if record_failure is None:
            self.record_failure(attempt.name, attempt.index, exc, kind)
        else:
            record_failure(attempt.name, attempt.index, exc)

    async def record_attempt_failure_async(
        self,
        attempt: ProviderAttempt,
        kind: str,
        exc: Exception,
        errors: list[str] | None = None,
    ) -> None:
        error_text = self._sanitized_error_text(exc)
        if errors is not None:
            errors.append(f"{attempt.name}: {error_text}")
        if is_provider_coverage_miss(exc) or isinstance(exc, ProviderCallBusyError):
            return
        await self.record_failure_async(attempt.name, attempt.index, exc, kind)

    def clear_cooldown(self, name: str, kind: str = "general") -> None:
        self._cooldowns.pop((name, kind), None)

    def _sanitized_error_text(self, exc: Exception) -> str:
        return sanitize_provider_error(
            _provider_error_text(exc),
            sensitive_values=_settings_sensitive_values(self.settings),
        )


def _settings_sensitive_values(settings: object) -> tuple[str, ...]:
    return tuple(value for name, value in vars(settings).items() if _sensitive_setting_name(name) and isinstance(value, str) and value)


def _sensitive_setting_name(name: str) -> bool:
    normalized = name.strip().lower().replace("-", "_")
    return normalized.endswith(("_token", "_api_key", "_password", "_secret", "_credential"))


def provider_source_name(provider: object, fallback: str) -> str:
    source = getattr(provider, "source_name", None)
    if isinstance(source, str) and source.strip():
        return source
    return fallback
