"""Lifecycle-owned, coalescing refresh requests from synchronous query workers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
import logging
from threading import Lock

from app.utils.clock import utc_now


class ProbabilityResearchRefreshCoordinator:
    """Own at most one refresh task and one queued event-loop notification."""

    def __init__(
        self, refresh: Callable[[], Awaitable[None]], report_failure: Callable[[Exception], Awaitable[None]],
        *, retry_seconds: float = 30.0, maximum_retry_seconds: float = 300.0,
    ) -> None:
        self._refresh = refresh
        self._report_failure = report_failure
        self._retry_seconds = retry_seconds
        self._maximum_retry_seconds = maximum_retry_seconds
        self._lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._epoch = 0
        self._requested = False
        self._notification_queued = False
        self._task: asyncio.Task[None] | None = None
        self._state = "stopped"
        self._attempt_count = 0
        self._failure_count = 0
        self._last_failure_type: str | None = None
        self._last_failure_at: str | None = None
        self._next_retry_at: str | None = None
        self._reporting_failed = False

    def start(self) -> Callable[[], bool]:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is None:
                if self._task is not None and not self._task.done():
                    raise RuntimeError("probability refresh coordinator is still stopping")
                self._epoch += 1
                self._loop = loop
                self._state = "idle"
            elif self._loop is not loop:
                raise RuntimeError("probability refresh coordinator belongs to another event loop")
            epoch = self._epoch
        return lambda: self._request(epoch)

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._requested or self._notification_queued or (self._task is not None and not self._task.done())

    @property
    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": self._state, "attempt_count": self._attempt_count, "failure_count": self._failure_count,
                "last_failure_type": self._last_failure_type, "last_failure_at": self._last_failure_at,
                "next_retry_at": self._next_retry_at, "reporting_failed": self._reporting_failed,
            }

    def _request(self, epoch: int) -> bool:
        with self._lock:
            if self._loop is None or epoch != self._epoch:
                return False
            self._requested = True
            if self._notification_queued or (self._task is not None and not self._task.done()):
                return True
            self._notification_queued = True
            self._state = "queued"
            try:
                self._loop.call_soon_threadsafe(self._dispatch, epoch)
            except RuntimeError:
                self._requested = self._notification_queued = False
                return False
            return True

    def _dispatch(self, epoch: int) -> None:
        with self._lock:
            if self._loop is None or epoch != self._epoch:
                return
            self._notification_queued = False
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run(epoch), name="market-scan-probability-request-refresh")
                self._task.add_done_callback(lambda task: self._finished(epoch, task))

    def _finished(self, epoch: int, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        with self._lock:
            if self._task is not task or self._epoch != epoch or self._loop is None:
                return
            self._task = None
            self._state = "idle"
            if self._requested and not self._notification_queued:
                self._notification_queued = True
                self._loop.call_soon(self._dispatch, epoch)

    def _take_request(self, epoch: int) -> bool:
        with self._lock:
            if self._loop is None or epoch != self._epoch or not self._requested:
                return False
            self._requested = False
            return True

    async def _run(self, epoch: int) -> None:
        delay = self._retry_seconds
        while self._take_request(epoch):
            with self._lock:
                self._state = "running"
                self._next_retry_at = None
                self._attempt_count += 1
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._record_failure(exc, delay)
                if not self._request(epoch):
                    return
                await asyncio.sleep(delay)
                delay = min(self._maximum_retry_seconds, delay * 2)
            else:
                delay = self._retry_seconds
                with self._lock:
                    self._failure_count = 0

    async def _record_failure(self, exc: Exception, delay: float) -> None:
        now = utc_now()
        with self._lock:
            self._state = "retry_wait"
            self._failure_count += 1
            self._last_failure_type = type(exc).__name__
            self._last_failure_at = now.isoformat()
            self._next_retry_at = (now + timedelta(seconds=delay)).isoformat()
        try:
            await self._report_failure(exc)
        except Exception as reporting_error:
            with self._lock:
                self._reporting_failed = True
            logging.getLogger(__name__).warning("Probability refresh failure reporting failed: %s", type(reporting_error).__name__)

    async def stop(self) -> None:
        with self._lock:
            self._epoch += 1
            self._loop = None
            self._state = "stopping"
            self._requested = self._notification_queued = False
            task = self._task
        if task is None:
            with self._lock:
                self._state = "stopped"
            return
        task.cancel()
        completed = asyncio.gather(task, return_exceptions=True)
        cancelled: asyncio.CancelledError | None = None
        while not completed.done():
            try:
                await asyncio.shield(completed)
            except asyncio.CancelledError as exc:
                cancelled = exc
        with self._lock:
            if self._task is task:
                self._task = None
                self._state = "stopped"
                self._next_retry_at = None
        if cancelled is not None:
            raise cancelled
