from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Response

from app.api.deps import get_datahub
from app.api.errors import run_sync_api_async
from app.models.discovery import (
    DiscoveryLeaderboardPage,
    DiscoveryPreset,
    DiscoveryPresetApplyRequest,
    DiscoveryPresetArchive,
    DiscoveryPresetCreate,
    DiscoveryPresetDeleteResponse,
    DiscoveryPresetPage,
    DiscoveryPresetRename,
    DiscoveryPresetUpdate,
    DiscoveryRankChangePage,
    DiscoveryResearchQueueRequest,
    DiscoveryResearchQueueResponse,
)
from app.services.datahub import DataHub
from app.services.discovery import DiscoveryService


router = APIRouter(prefix="/api/discovery", tags=["discovery"])


def get_discovery_service(datahub: DataHub = Depends(get_datahub)) -> DiscoveryService:
    return datahub.cache.discovery_service


@router.post("/presets", response_model=DiscoveryPreset, status_code=201)
async def create_discovery_preset(
    payload: DiscoveryPresetCreate,
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPreset:
    return await run_sync_api_async(lambda: service.create_preset(payload))


@router.post("/presets/import", response_model=DiscoveryPreset, status_code=201)
async def import_discovery_preset(
    payload: DiscoveryPresetArchive,
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPreset:
    return await run_sync_api_async(lambda: service.import_preset(payload))


@router.get("/presets", response_model=DiscoveryPresetPage)
async def list_discovery_presets(
    response: Response,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPresetPage:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.list_presets(page=page, page_size=page_size))


@router.get("/presets/{preset_id}", response_model=DiscoveryPreset)
async def get_discovery_preset(
    response: Response,
    preset_id: int = Path(ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPreset:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.get_preset(preset_id))


@router.patch("/presets/{preset_id}", response_model=DiscoveryPreset)
async def rename_discovery_preset(
    payload: DiscoveryPresetRename,
    preset_id: int = Path(ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPreset:
    return await run_sync_api_async(lambda: service.rename_preset(preset_id, payload))


@router.put("/presets/{preset_id}", response_model=DiscoveryPreset)
async def update_discovery_preset(
    payload: DiscoveryPresetUpdate,
    preset_id: int = Path(ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPreset:
    return await run_sync_api_async(lambda: service.update_preset(preset_id, payload))


@router.delete("/presets/{preset_id}", response_model=DiscoveryPresetDeleteResponse)
async def delete_discovery_preset(
    preset_id: int = Path(ge=1),
    expected_revision: int = Query(..., ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPresetDeleteResponse:
    return await run_sync_api_async(
        lambda: service.delete_preset(preset_id, expected_revision=expected_revision)
    )


@router.get("/presets/{preset_id}/export", response_model=DiscoveryPresetArchive)
async def export_discovery_preset(
    response: Response,
    preset_id: int = Path(ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPresetArchive:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.export_preset(preset_id))


@router.post("/presets/{preset_id}/apply", response_model=DiscoveryLeaderboardPage)
async def apply_discovery_preset(
    payload: DiscoveryPresetApplyRequest,
    preset_id: int = Path(ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryLeaderboardPage:
    return await run_sync_api_async(
        lambda: service.apply_preset(
            preset_id,
            run_id=payload.run_id,
            page=payload.page,
            page_size=payload.page_size,
        )
    )


@router.post(
    "/presets/{preset_id}/research-queue",
    response_model=DiscoveryResearchQueueResponse,
)
async def enqueue_discovery_research(
    payload: DiscoveryResearchQueueRequest,
    preset_id: int = Path(ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryResearchQueueResponse:
    return await run_sync_api_async(lambda: service.enqueue_research(preset_id, payload))


@router.get("/runs/{run_id}/rank-changes", response_model=DiscoveryRankChangePage)
async def discovery_rank_changes(
    response: Response,
    run_id: int = Path(ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryRankChangePage:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.rank_changes(run_id, page=page, page_size=page_size))


__all__ = ["get_discovery_service", "router"]
