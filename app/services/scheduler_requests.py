"""Scheduler-owned manual requests survive cancellation of their HTTP waiter."""

from __future__ import annotations

import asyncio
from functools import partial

from app.models.system import TaskRunRequestReceipt
from app.repositories.task_requests import validate_task_request_id
from app.services.lifecycle_cleanup import await_cleanup
from app.services.scheduler_contracts import INSTANCE_GUARD_BUSY_MESSAGE, SchedulerRuntimeContext
from app.services.scheduler_helpers import consume_future_exception, offload, task_error_message


MAX_PENDING_MANUAL_REQUESTS = 16


class SchedulerRequestMixin(SchedulerRuntimeContext):
    async def submit_run_once(self, task_name: str | None, request_id: str) -> TaskRunRequestReceipt:
        validate_task_request_id(request_id)
        if task_name is not None and task_name not in self.tasks:
            raise ValueError("该入口仅支持已注册的本地刷新任务")
        existing = self._manual_requests.get(request_id)
        if existing is not None:
            if existing[0] != task_name:
                raise ValueError("该请求编号已绑定其他任务")
            return await asyncio.shield(existing[1])
        if len(self._manual_requests) >= MAX_PENDING_MANUAL_REQUESTS:
            raise RuntimeError("待处理手动任务过多，请先核对已有回执")
        ready = asyncio.get_running_loop().create_future()
        ready.add_done_callback(consume_future_exception)
        self._manual_requests[request_id] = (task_name, ready)
        worker = asyncio.create_task(self._run_manual_request(task_name, request_id, ready), name="scheduler-manual-request")
        self._active_tasks.add(worker)
        self._quiescent_event.clear()
        worker.add_done_callback(partial(self._manual_request_done, request_id, ready))
        return await asyncio.shield(ready)

    async def read_run_once_receipt(self, request_id: str) -> TaskRunRequestReceipt:
        receipt = await offload(self.datahub.cache.runtime_event_repo.task_request, request_id)
        if receipt.status not in {"accepted", "running"} or request_id in self._manual_requests:
            return receipt
        probe = getattr(self._instance_guard, "held_by_other", None)
        if not callable(probe):
            return receipt.model_copy(update={"status": "unknown", "messages": ["无法确认执行所有权，请稍后核对"]})
        try:
            held_elsewhere = await offload(probe)
        except OSError:
            return receipt.model_copy(update={"status": "unknown", "messages": ["暂时无法读取执行所有权，请稍后核对"]})
        if self._guard_acquired or not held_elsewhere:
            # The owner may have persisted completion between the first read
            # and its lease release. Never turn that completed receipt into an interruption.
            receipt = await offload(self.datahub.cache.runtime_event_repo.task_request, request_id)
            if receipt.status not in {"accepted", "running"} or request_id in self._manual_requests:
                return receipt
            return receipt.model_copy(update={"status": "interrupted", "messages": ["执行所有权已结束，最终结果未完整确认"]})
        return receipt

    def _manual_request_done(self, request_id, ready, worker) -> None:
        self._manual_requests.pop(request_id, None)
        self._active_task_done(worker)
        if not ready.done():
            error = None if worker.cancelled() else worker.exception()
            if isinstance(error, ValueError):
                ready.set_exception(error)
            else:
                ready.set_exception(RuntimeError("任务请求未确认，请查询该请求回执"))
        self._mark_quiescent_if_idle()

    async def _admit_manual_request(self, task_name, request_id):
        async with self._manual_request_admission_lock:
            repository = self.datahub.cache.runtime_event_repo
            existing = await offload(repository.find_task_request, request_id, task_name)
            if existing is not None:
                return existing, False, False
            if not await self._begin_manual_guard_use():
                raise RuntimeError(INSTANCE_GUARD_BUSY_MESSAGE)
            try:
                if self._manual_guard_users == 1 and not self._scheduler_guard_active:
                    await offload(repository.reconcile_task_requests)
                receipt, created = await offload(repository.reserve_task_request, request_id, task_name)
                return receipt, created, True
            except BaseException:
                await self._end_manual_guard_use()
                raise

    async def _run_manual_request(self, task_name, request_id, ready) -> None:
        receipt, created, leased = await self._admit_manual_request(task_name, request_id)
        try:
            ready.set_result(receipt)
            if created:
                await self._execute_manual_request(task_name, request_id)
        finally:
            if leased:
                await self._end_manual_guard_use()

    async def _execute_manual_request(self, task_name, request_id) -> None:
        repository = self.datahub.cache.runtime_event_repo
        try:
            messages = await self.run_once(task_name, request_id=request_id)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(offload(repository.finish_task_request, request_id, "interrupted", ["任务执行被中断，请核对实际运行记录"]))
            await await_cleanup(cleanup)
            raise
        except Exception as exc:
            await offload(repository.finish_task_request, request_id, "failed", [task_error_message(exc)])
        else:
            await offload(repository.finish_task_request, request_id, "completed", messages)


__all__ = ["SchedulerRequestMixin"]
