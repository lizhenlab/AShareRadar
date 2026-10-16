from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import get_datahub
from app.api.errors import run_sync_api_async
from app.models.workspace_maintenance import MaintenanceStorageReport, WorkspaceMaintenanceReport
from app.services.datahub import DataHub
from app.services.workspace_maintenance import workspace_maintenance_status, workspace_maintenance_storage


router = APIRouter()
_STORAGE_INSPECTION = threading.Lock()


@router.get("/api/system/workspace-maintenance", response_model=WorkspaceMaintenanceReport)
async def maintenance_status(response: Response, datahub: DataHub = Depends(get_datahub)) -> WorkspaceMaintenanceReport:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: workspace_maintenance_status(datahub.cache.path))


@router.post("/api/system/workspace-maintenance/storage", response_model=MaintenanceStorageReport)
async def maintenance_storage(
    response: Response, scope_digest: str = Query(pattern=r"^[a-f0-9]{64}$"), datahub: DataHub = Depends(get_datahub),
) -> MaintenanceStorageReport:
    response.headers["Cache-Control"] = "no-store"

    def inspect() -> MaintenanceStorageReport:
        if not _STORAGE_INSPECTION.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="容量检查正在执行，请稍后重试",
                                headers={"Retry-After": "2", "Cache-Control": "no-store"})
        try:
            return workspace_maintenance_storage(datahub.cache.path, scope_digest)
        finally:
            _STORAGE_INSPECTION.release()

    try:
        return await run_sync_api_async(inspect)
    except HTTPException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail,
                            headers={**(exc.headers or {}), "Cache-Control": "no-store"}) from exc
