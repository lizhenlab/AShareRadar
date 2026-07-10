from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_datahub, get_scheduler
from app.api.errors import run_api, run_sync_api
from app.models.schemas import MonitorEvent, SchedulerStatus, SystemDiagnostics, TaskRun, TaskRunOnceResponse
from app.services.datahub import DataHub
from app.services.scheduler import LocalDataScheduler
from app.services.system_diagnostics import build_system_diagnostics


router = APIRouter()


@router.get("/api/tasks/status", response_model=SchedulerStatus)
async def task_status(scheduler: LocalDataScheduler = Depends(get_scheduler)) -> SchedulerStatus:
    return run_sync_api(scheduler.status)


@router.get("/api/tasks/runs", response_model=list[TaskRun])
async def task_runs(
    limit: int = Query(20, ge=1, le=100),
    datahub: DataHub = Depends(get_datahub),
) -> list[TaskRun]:
    return run_sync_api(lambda: datahub.cache.recent_task_runs(limit=limit))


@router.get("/api/monitor/events", response_model=list[MonitorEvent])
async def monitor_events(
    limit: int = Query(30, ge=1, le=200),
    datahub: DataHub = Depends(get_datahub),
) -> list[MonitorEvent]:
    return run_sync_api(lambda: datahub.cache.recent_monitor_events(limit=limit))


@router.post("/api/tasks/run-once", response_model=TaskRunOnceResponse)
async def run_task_once(
    task: str | None = Query(None, description="任务名称，不填则按顺序执行全部本地刷新任务"),
    scheduler: LocalDataScheduler = Depends(get_scheduler),
) -> TaskRunOnceResponse:
    async def run() -> TaskRunOnceResponse:
        messages = await scheduler.run_once(task)
        return TaskRunOnceResponse(ok=True, messages=messages)

    return await run_api(run)


@router.get("/api/system/diagnostics", response_model=SystemDiagnostics)
async def system_diagnostics(
    datahub: DataHub = Depends(get_datahub),
    scheduler: LocalDataScheduler = Depends(get_scheduler),
) -> SystemDiagnostics:
    return run_sync_api(lambda: build_system_diagnostics(datahub, scheduler))
