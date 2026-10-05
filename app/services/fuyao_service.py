"""Optional research collection owned and closed by the existing DataHub."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
from typing import Any, TypeVar
from uuid import uuid4

from app.config import Settings
from app.models.fuyao import FinancialReportBundle
from app.models.fuyao_research import FuyaoJob, FuyaoJobProgress, FuyaoJobRequest
from app.models.fuyao_scoring import FuyaoValuationScore
from app.models.market import ProviderCapability
from app.repositories.fuyao_research import FuyaoResearchRepository
from app.services.datahub_runtime import ProviderRuntime
from app.services.fuyao_client import FuyaoClient
from app.services.fuyao_contracts import FuyaoError
from app.services.fuyao_dumps_validation import FuyaoDumpError
from app.services.fuyao_fetch import fetch_financials, fetch_sentiment
from app.services.fuyao_observations import canonical_stock, normalized_valuations
from app.services.fuyao_job_runtime import ACTIVE_JOB, ACTIVE_STATUSES, FuyaoJobExecution, retry_request
from app.services.fuyao_sync_control import FuyaoSyncCancelled
from app.services.fuyao_sectors import canonical_index, fetch_sectors
from app.services.fuyao_scoring import build_fuyao_valuation_score
from app.services.lifecycle_cleanup import await_cleanup
from app.utils.audit_time import audit_now_text
from app.utils.clock import market_now


T = TypeVar("T")


async def fuyao_io(call: Callable[..., T], *args: Any) -> T:
    worker = asyncio.create_task(asyncio.to_thread(call, *args))
    return await await_cleanup(worker)


class FuyaoPersistentBudget:
    def __init__(self, repository: FuyaoResearchRepository, limit: int) -> None:
        self.repository, self.limit = repository, limit

    async def reserve(self) -> None:
        try:
            await fuyao_io(self.repository.reserve_request, market_now().date().isoformat(), self.limit)
        except RuntimeError:
            raise FuyaoError("daily_request_limit") from None


class FuyaoService:
    source_name = "同花顺扶摇"

    def __init__(self, settings: Settings, runtime: ProviderRuntime, *, client: FuyaoClient | None = None) -> None:
        self.settings, self.runtime = settings, runtime
        self.root = settings.cache_path.with_suffix(".fuyao")
        self.repository = FuyaoResearchRepository(self.root / "research.sqlite3")
        budget = FuyaoPersistentBudget(self.repository, settings.fuyao_daily_request_limit)
        self.client = client if client is not None else FuyaoClient(settings, budget=budget)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._job_records: dict[str, FuyaoJob] = {}
        self._executions: dict[str, FuyaoJobExecution] = {}
        self._close_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._recovered = False

    @property
    def has_active_jobs(self) -> bool:
        """Inspect live local workers without recovering jobs or reading account state."""
        return any(not task.done() for task in self._tasks.values())

    def capability(self) -> ProviderCapability:
        return ProviderCapability(name="fuyao", installed=True, enabled=self.settings.fuyao_enabled,
                                  reliability_level="授权数据源", note="财报、估值、板块与情绪研究；正式行情资格未开放")

    async def request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        execution = ACTIVE_JOB.get()
        key = (execution.job.id if execution else None, path, tuple(sorted((params or {}).items())))
        async def start() -> dict[str, Any]:
            if execution is not None:
                return await execution.request(lambda: self.client.request(path, params))
            return await self.client.request(path, params)
        return await self.runtime.call_provider("fuyao", "research", start, request_key=key, timeout_seconds=180)

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            await self._recover_jobs()
        jobs = await fuyao_io(self.repository.jobs)
        merged = {job.id: job for job in jobs}
        merged.update(self._job_records)
        jobs = sorted(merged.values(), key=lambda job: job.created_at, reverse=True)[:20]
        calls = await fuyao_io(self.repository.request_count, market_now().date().isoformat())
        return {**self.client.status(), "persistent_daily_requests": calls, "billing_status": "以账号费用条款为准",
                "budget_scope": "当前项目数据目录每日请求上限，包含重试；不是供应商账户额度",
                "jobs": [item.model_dump(mode="json") for item in jobs], "active_jobs": list(self._tasks),
                "download_hosts_configured": bool(self.settings.fuyao_download_hosts)}

    async def financials(self, symbol: str) -> FinancialReportBundle | None:
        return await fuyao_io(self.repository.financials, canonical_stock(symbol))

    async def valuation_score(self, symbol: str, evaluated_at: str) -> FuyaoValuationScore:
        normalized = canonical_stock(symbol)
        observation = await fuyao_io(self.repository.latest, "valuations", normalized)
        return build_fuyao_valuation_score(normalized, observation, evaluated_at)

    async def start_job(self, request: FuyaoJobRequest) -> FuyaoJob:
        request = _normalized_request(request)
        async with self._lock:
            return await self._start_locked(request)

    async def _start_locked(self, request: FuyaoJobRequest, parent_id: str | None = None) -> FuyaoJob:
        if self._closed:
            raise RuntimeError("扶摇研究服务已关闭")
        if not self.settings.fuyao_enabled or not self.client.status()["configured"]:
            raise ValueError("请先配置并启用扶摇 API Key")
        await self._recover_jobs()
        if self._tasks:
            raise RuntimeError("已有扶摇同步任务运行中，请等待完成")
        now = audit_now_text()
        job = FuyaoJob(id=uuid4().hex, kind=request.kind, status="running", created_at=now, updated_at=now,
                      request=request, parent_job_id=parent_id,
                      total=len(request.symbols) if request.kind in {"financials", "valuations"} else 1)
        return await await_cleanup(asyncio.create_task(self._persist_and_launch(job, request)))

    async def get_job(self, job_id: str) -> FuyaoJob:
        async with self._lock:
            await self._recover_jobs()
            return await self._find_job(job_id)

    async def _find_job(self, job_id: str) -> FuyaoJob:
        job = self._job_records.get(job_id) or await fuyao_io(self.repository.job, job_id)
        if job is None:
            raise LookupError("扶摇同步任务不存在")
        return job.model_copy(deep=True)

    async def retry_job(self, job_id: str) -> FuyaoJob:
        async with self._lock:
            await self._recover_jobs()
            parent = await self._find_job(job_id)
            child = await fuyao_io(self.repository.retry_child, job_id)
            if child is not None:
                return await self._find_job(child.id)
            request = _normalized_request(retry_request(parent))
            return await self._start_locked(request, parent.id)

    async def cancel_job(self, job_id: str) -> FuyaoJob:
        async with self._lock:
            await self._recover_jobs()
            job = await self._find_job(job_id)
            if job.status not in ACTIVE_STATUSES:
                return job
            execution = self._executions.get(job_id)
            if execution is None:
                raise RuntimeError("任务不属于当前进程，请刷新本地状态")
            operation = asyncio.create_task(self._cancel_execution(execution))
            return await await_cleanup(operation)

    async def _cancel_execution(self, execution: FuyaoJobExecution) -> FuyaoJob:
        async with execution.lock:
            job = execution.job
            if job.status != "running":
                return job.model_copy(deep=True)
            task = self._tasks[job.id]
            if not execution.collection_done and not (job.total > 0 and job.completed >= job.total):
                update = job.model_copy(update={"status": "cancelling", "updated_at": audit_now_text(),
                                                "message": "正在停止，等待已开始的请求和本地写入收尾"})
                await fuyao_io(self.repository.save_job, update)
                job.status, job.updated_at, job.message = update.status, update.updated_at, update.message
                execution.stop(task)
                return job.model_copy(deep=True)
        await await_cleanup(asyncio.create_task(_join_job(task)))
        return job.model_copy(deep=True)

    async def _persist_and_launch(self, job: FuyaoJob, request: FuyaoJobRequest) -> FuyaoJob:
        await fuyao_io(self.repository.save_job, job)
        self._job_records[job.id] = job
        self._executions[job.id] = FuyaoJobExecution(job)
        while len(self._job_records) > 100:
            self._job_records.pop(next(iter(self._job_records)))
        result = job.model_copy(deep=True)
        task = asyncio.create_task(self._run_job(job, request), name=f"fuyao-{request.kind}")
        self._tasks[job.id] = task
        task.add_done_callback(lambda completed: self._finished(job.id, completed))
        return result

    async def _recover_jobs(self) -> None:
        if self._recovered:
            return
        for job in await fuyao_io(self.repository.unfinished_jobs):
            job.status, job.finished_at, job.message = "interrupted", audit_now_text(), "上次进程结束，同步未确认完成"
            job.updated_at = job.finished_at
            await fuyao_io(self.repository.save_job, job)
        self._recovered = True

    async def _run_job(self, job: FuyaoJob, request: FuyaoJobRequest) -> None:
        execution = self._executions[job.id]
        execution.started = True
        token = ACTIVE_JOB.set(execution)
        status, message = "failed", "同步未确认完成"
        try:
            execution.control.checkpoint()
            await self._collect(job, request)
            execution.collection_done = True
            async with execution.lock:
                if job.status == "cancelling":
                    raise FuyaoSyncCancelled()
                status = ("degraded" if job.completed else "failed") if job.errors else "completed"
                message = f"已保存 {job.completed}/{job.total} 项；记录用于研究观察"
        except asyncio.CancelledError:
            status, message = "cancelled", "同步已停止，已发布记录保持可读"
            raise
        except FuyaoSyncCancelled:
            status, message = "cancelled", "同步已停止，已发布记录保持可读"
        except Exception as exc:
            status, message = "failed", _safe_job_error(exc)
        finally:
            await await_cleanup(asyncio.create_task(self._finish_execution(execution, status, message)))
            ACTIVE_JOB.reset(token)

    async def _finish_execution(self, execution: FuyaoJobExecution, status: str, message: str) -> None:
        await execution.drain()
        async with execution.lock:
            job = execution.job
            terminal = "cancelled" if job.status == "cancelling" else status
            if execution.progress_error and job.status != "cancelling":
                terminal = "completed" if job.completed >= job.total else "failed"
            job.status = FuyaoJob.model_validate({**job.model_dump(), "status": terminal}).status
            job.message = "同步已停止，已发布记录保持可读" if terminal == "cancelled" else message
            if execution.progress_error:
                job.errors.append("同步进度未能持久保存，请检查本地存储")
                if job.status != "cancelled":
                    job.status = "completed" if job.completed >= job.total else "failed"
                job.message = "数据已发布，进度记录保存失败" if job.completed >= job.total else "进度记录保存失败，已停止同步；已发布记录保持可读"
            job.finished_at = job.updated_at = audit_now_text()
            await self._persist_terminal_job(job)

    async def _persist_terminal_job(self, job: FuyaoJob) -> None:
        try:
            await fuyao_io(self.repository.save_job, job)
        except Exception:
            job.status, job.message = "failed", "任务结束状态未能持久保存，请检查本地存储"

    async def _checkpoint(self, job: FuyaoJob) -> None:
        execution = self._executions[job.id]
        async with execution.lock:
            job.updated_at = audit_now_text()
            await fuyao_io(self.repository.save_job, job)

    async def _collect(self, job: FuyaoJob, request: FuyaoJobRequest) -> None:
        if request.kind == "financials":
            await self._collect_financials(job, request)
        elif request.kind == "valuations":
            await self._collect_valuations(job, request.symbols)
        elif request.kind == "sectors":
            await self._save_item(job, "sectors", "market", await fetch_sectors(self, request.index_symbols))
        elif request.kind == "sentiment":
            await self._save_item(job, "sentiment", "market", await fetch_sentiment(self, request.symbols))
        else:
            await self._collect_history(job, request.kind == "history_full")

    async def _collect_financials(self, job: FuyaoJob, request: FuyaoJobRequest) -> None:
        for symbol in request.symbols:
            try:
                payload = await fetch_financials(self, symbol, request)
                await self._save_item(job, "financials", symbol, payload)
            except FuyaoError as exc:
                job.errors.append(f"{symbol}: {_safe_job_error(exc)}")
                if not _symbol_rejection(exc):
                    raise
            except (ValueError, KeyError, TypeError) as exc:
                job.errors.append(f"{symbol}: {_safe_job_error(exc)}")
            job.message = f"财报已保存 {job.completed}/{job.total} 只"
            await self._checkpoint(job)

    async def _collect_valuations(self, job: FuyaoJob, symbols: list[str]) -> None:
        await self._collect_valuation_batch(job, symbols)

    async def _collect_valuation_batch(self, job: FuyaoJob, symbols: list[str]) -> None:
        try:
            payload = await self.request("/api/a-share/valuations/snapshot", {"thscodes": ",".join(symbols)})
        except FuyaoError as exc:
            if _symbol_rejection(exc) and len(symbols) > 1:
                for selected in _valuation_subsets(symbols):
                    await self._collect_valuation_batch(job, selected)
                return
            job.errors.append(f"{','.join(symbols)}: {_safe_job_error(exc)}")
            if not _symbol_rejection(exc):
                raise
            return
        rows = normalized_valuations(payload, symbols)
        for row in rows:
            await self._save_item(job, "valuations", row["symbol"], row)
        if len(rows) < len(symbols):
            missing = sorted(set(symbols) - {row["symbol"] for row in rows})
            job.errors.append(f"{','.join(missing)}: 未返回估值，原记录保持，缺项不补零")
        job.message = f"估值已保存 {job.completed}/{job.total} 只"
        await self._checkpoint(job)

    async def _collect_history(self, job: FuyaoJob, full: bool) -> None:
        from app.services.fuyao_dumps import sync_market_dumps
        execution = self._executions[job.id]
        done = asyncio.Event()
        monitor = asyncio.create_task(self._monitor_history(execution, done))
        try:
            await sync_market_dumps(self, self.root / "history", "full" if full else "incremental",
                                    allowed_download_hosts=self.settings.fuyao_download_hosts, control=execution.control)
            job.completed = 1
        finally:
            done.set()
            await await_cleanup(monitor)

    async def _monitor_history(self, execution: FuyaoJobExecution, done: asyncio.Event) -> None:
        while True:
            async with execution.lock:
                progress = FuyaoJobProgress.model_validate(execution.control.snapshot())
                job = execution.job
                if job.status in ACTIVE_STATUSES and progress != job.progress:
                    job.progress, job.updated_at = progress, audit_now_text()
                    try:
                        await fuyao_io(self.repository.save_job, job)
                    except Exception:
                        execution.progress_error = True
                        if job.completed < job.total:
                            execution.stop(self._tasks[job.id])
                        return
            if done.is_set():
                return
            try:
                await asyncio.wait_for(done.wait(), 0.5)
            except TimeoutError:
                continue

    async def _save_item(self, job: FuyaoJob, capability: str, symbol: str, payload: dict[str, Any]) -> None:
        await await_cleanup(asyncio.create_task(self._publish_item(job, capability, symbol, payload)))

    async def _publish_item(self, job: FuyaoJob, capability: str, symbol: str, payload: dict[str, Any]) -> None:
        execution = self._executions[job.id]
        async with execution.lock:
            execution.control.checkpoint()
            updated = await fuyao_io(self.repository.publish_item, job, capability, symbol, audit_now_text(), payload)
            job.completed, job.completed_symbols, job.updated_at = updated.completed, updated.completed_symbols, updated.updated_at

    def _finished(self, job_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(job_id, None)
        self._executions.pop(job_id, None)
        if not task.cancelled():
            task.exception()

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_owned())
        await await_cleanup(self._close_task)

    async def _close_owned(self) -> None:
        async with self._lock:
            self._closed = True
            tasks = tuple(self._tasks.values())
            executions = tuple(self._executions.values())
            for execution in executions:
                try:
                    await self._cancel_execution(execution)
                except Exception:
                    # Shutdown still owns and must stop calls if the intent write fails.
                    execution.stop(self._tasks[execution.job.id])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()


def _normalized_request(request: FuyaoJobRequest) -> FuyaoJobRequest:
    symbols = list(dict.fromkeys(canonical_stock(symbol) for symbol in request.symbols))
    indices = list(dict.fromkeys(canonical_index(symbol) for symbol in request.index_symbols))
    if request.kind in {"financials", "valuations"} and not symbols:
        raise ValueError("请选择至少一只研究股票")
    if request.report is not None:
        year, quarter = (int(value) for value in request.report.split("-"))
        month, day = ((3, 31), (6, 30), (9, 30), (12, 31))[quarter - 1]
        if date(year, month, day) > market_now().date():
            raise ValueError("报告期不能位于未来")
    return request.model_copy(update={"symbols": symbols, "index_symbols": indices})


def _safe_job_error(exc: Exception) -> str:
    if isinstance(exc, FuyaoError):
        return f"{exc} (code={exc.code})" if exc.code is not None else str(exc)
    if isinstance(exc, FuyaoDumpError):
        return str(exc)
    return "数据同步或校验未通过，未发布本项结果；请核对接口权限、数据契约和本地文件"


def _symbol_rejection(exc: FuyaoError) -> bool:
    return exc.category == "business_error" and exc.code in {1002, 3001, 3004}


def _valuation_subsets(symbols: list[str]) -> list[list[str]]:
    """Each error split strictly shrinks; at most 2*N-1 calls for N symbols."""
    groups: dict[str, list[str]] = {}
    for symbol in symbols:
        groups.setdefault(symbol.rsplit(".", 1)[1], []).append(symbol)
    if len(groups) > 1:
        return list(groups.values())
    middle = len(symbols) // 2
    return [symbols[:middle], symbols[middle:]]


async def _join_job(task: asyncio.Task[None]) -> None:
    await asyncio.gather(task, return_exceptions=True)
