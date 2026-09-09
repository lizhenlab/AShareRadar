from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from app.api.deps import get_domain_services
from app.api.errors import no_store_http_exception, run_sync_api_async
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
from app.models.market_scan_screen_alert import (
    MarketScanScreenAlertDetailPage,
    MarketScanScreenAlertHistoryKind,
    MarketScanScreenAlertHistoryPage,
    MarketScanScreenAlertRequest,
    MarketScanScreenAlertResponse,
)
from app.services.discovery import DiscoveryService
from app.services.domain_service_bundle import DomainServiceBundle


router = APIRouter(prefix="/api/discovery", tags=["discovery"])


def get_discovery_service(
    services: DomainServiceBundle = Depends(get_domain_services),
) -> DiscoveryService:
    return services.discovery


@router.post("/presets", response_model=DiscoveryPreset, status_code=201)
async def create_discovery_preset(
    payload: DiscoveryPresetCreate,
    response: Response,
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPreset:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.create_preset(payload))


@router.post("/presets/import", response_model=DiscoveryPreset, status_code=201)
async def import_discovery_preset(
    payload: DiscoveryPresetArchive,
    response: Response,
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPreset:
    response.headers["Cache-Control"] = "no-store"
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
    response: Response,
    preset_id: int = Path(ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPreset:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.rename_preset(preset_id, payload))


@router.put("/presets/{preset_id}", response_model=DiscoveryPreset)
async def update_discovery_preset(
    payload: DiscoveryPresetUpdate,
    response: Response,
    preset_id: int = Path(ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPreset:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.update_preset(preset_id, payload))


@router.delete("/presets/{preset_id}", response_model=DiscoveryPresetDeleteResponse)
async def delete_discovery_preset(
    response: Response,
    preset_id: int = Path(ge=1),
    expected_revision: int = Query(..., ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryPresetDeleteResponse:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.delete_preset(preset_id, expected_revision=expected_revision))


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
    response: Response,
    preset_id: int = Path(ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryLeaderboardPage:
    response.headers["Cache-Control"] = "no-store"
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
    response: Response,
    preset_id: int = Path(ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> DiscoveryResearchQueueResponse:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.enqueue_research(preset_id, payload))


@router.post(
    "/presets/{preset_id}/screen-alerts",
    response_model=MarketScanScreenAlertResponse,
)
async def record_discovery_screen_alert(
    payload: MarketScanScreenAlertRequest,
    response: Response,
    preset_id: int = Path(ge=1),
    service: DiscoveryService = Depends(get_discovery_service),
) -> MarketScanScreenAlertResponse:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.record_screen_alert(preset_id, payload))


@router.get("/presets/{preset_id}/screen-alerts", response_model=MarketScanScreenAlertHistoryPage)
async def discovery_screen_alert_history(
    response: Response, preset_id: int = Path(ge=1),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    service: DiscoveryService = Depends(get_discovery_service),
) -> MarketScanScreenAlertHistoryPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await run_sync_api_async(lambda: service.screen_alert_history(preset_id, page=page, page_size=page_size))
    except HTTPException as exc:
        raise no_store_http_exception(exc) from exc


@router.get("/presets/{preset_id}/screen-alerts/{event_id}", response_model=MarketScanScreenAlertDetailPage)
async def discovery_screen_alert_detail(
    response: Response, preset_id: int = Path(ge=1), event_id: int = Path(ge=1),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
    kind: MarketScanScreenAlertHistoryKind = Query("all"),
    service: DiscoveryService = Depends(get_discovery_service),
) -> MarketScanScreenAlertDetailPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await run_sync_api_async(lambda: service.screen_alert_detail(preset_id, event_id, page=page, page_size=page_size, kind=kind))
    except HTTPException as exc:
        raise no_store_http_exception(exc) from exc


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
