from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
import threading
from typing import Protocol


class TaskRunCache(Protocol):
    def start_task_run(self, task_name: str) -> int:
        ...

    def finish_task_run(self, run_id: int, status: str, message: str | None = None) -> None:
        ...


class _TaskRunStartHandoff:
    def __init__(self, cache: TaskRunCache, task_name: str, cancel_message: str) -> None:
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
                "cancelled",
                self.cancel_message,
            )

    def claim(self) -> None:
        self._decision.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._decision.set()


async def start_task_run_cancel_safe(
    cache: TaskRunCache,
    task_name: str,
    cancel_message: str,
) -> int:
    """Create a task-run row without losing a late thread result to cancellation."""
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


def _consume_future_exception(future: asyncio.Future) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except asyncio.CancelledError:
        pass


def _finish_task_run_quietly(
    cache: TaskRunCache,
    run_id: int,
    status: str,
    message: str,
) -> None:
    try:
        cache.finish_task_run(run_id, status, message)
    except Exception:
        pass


__all__ = ["start_task_run_cancel_safe"]
