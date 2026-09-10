"""Owned task state and provider calls for one explicit Fuyao research job."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.models.fuyao_research import FuyaoJob, FuyaoJobRequest
from app.services.fuyao_sync_control import FuyaoSyncControl
from app.services.lifecycle_cleanup import await_cleanup


T = TypeVar("T")
ACTIVE_JOB: ContextVar[FuyaoJobExecution | None] = ContextVar("fuyao_active_job", default=None)
ACTIVE_STATUSES = frozenset({"running", "cancelling"})


@dataclass
class FuyaoJobExecution:
    job: FuyaoJob
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    control: FuyaoSyncControl = field(default_factory=FuyaoSyncControl)
    calls: set[asyncio.Task[Any]] = field(default_factory=set)
    started: bool = False
    collection_done: bool = False
    progress_error: bool = False

    def stop(self, task: asyncio.Task[None]) -> None:
        self.control.cancel()
        for call in tuple(self.calls):
            call.cancel()
        # A never-started coroutine cannot run its finally block after cancel().
        if self.started:
            task.cancel()

    async def request(self, start: Callable[[], Coroutine[Any, Any, T]]) -> T:
        self.control.checkpoint()
        call = asyncio.create_task(start())
        self.calls.add(call)
        try:
            return await call
        finally:
            if not call.done():
                call.cancel()
            await await_cleanup(asyncio.create_task(_drain_calls((call,))))
            self.calls.discard(call)

    async def drain(self) -> None:
        calls = tuple(self.calls)
        for call in calls:
            call.cancel()
        await await_cleanup(asyncio.create_task(_drain_calls(calls)))


async def _drain_calls(calls: tuple[asyncio.Task[Any], ...]) -> None:
    if calls:
        await asyncio.gather(*calls, return_exceptions=True)


def retry_request(parent: FuyaoJob) -> FuyaoJobRequest:
    if parent.status in ACTIVE_STATUSES:
        raise RuntimeError("任务仍在执行或停止中，请等待收尾")
    if parent.request is None:
        raise RuntimeError("旧任务未保存同步输入，请重新选择参数创建任务")
    if parent.status == "completed":
        raise RuntimeError("该任务已完成，无需补做")
    request = parent.request.model_copy(deep=True)
    if request.kind in {"financials", "valuations"}:
        request.symbols = [symbol for symbol in request.symbols if symbol not in parent.completed_symbols]
        if not request.symbols:
            raise RuntimeError("该任务股票已全部保存，无需补做")
    return request
