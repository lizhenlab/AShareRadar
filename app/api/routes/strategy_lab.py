"""HTTP API for the evidence-first full-market strategy laboratory."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Response

from app.api.deps import get_datahub
from app.api.errors import run_sync_api_async
from app.models.strategy_lab import (
    StrategyCompileRequest,
    StrategyCompileResponse,
    StrategyMetricDefinition,
    StrategyNaturalLanguageRequest,
    StrategyNaturalLanguageResponse,
    StrategySpec,
    StrategySpecArchiveRequest,
    StrategySpecCopyRequest,
    StrategySpecCreate,
    StrategySpecPage,
    StrategySpecUpdate,
    StrategyVersionDiff,
    StrategyVersionPage,
)
from app.models.strategy_execution import (
    PortfolioCandidatePage,
    PortfolioCandidateStatus,
    PortfolioCandidateSort,
    PortfolioDraft,
    StrategyExecutionComparison,
    StrategyExecutionPage,
    StrategyExecutionRequest,
)
from app.models.strategy_evidence import (
    StrategyEvidenceCenter,
    StrategyEvidenceRefreshRequest,
)
from app.models.strategy_automation import (
    StrategyAlertEventPage,
    StrategyAutomationRunSummary,
    StrategySchedule,
    StrategyScheduleCreate,
    StrategySchedulePage,
    StrategyScheduleUpdate,
    StrategySimulationPlan,
)
from app.services.datahub import DataHub
from app.services.strategy_execution import StrategyExecutionService
from app.services.strategy_evidence import StrategyEvidenceService
from app.services.strategy_automation import StrategyAutomationService
from app.services.strategy_lab import StrategyLabService


router = APIRouter(prefix="/api/strategy-lab", tags=["strategy-lab"])


def get_strategy_lab_service(datahub: DataHub = Depends(get_datahub)) -> StrategyLabService:
    return datahub.cache.strategy_lab_service


def get_strategy_execution_service(
    datahub: DataHub = Depends(get_datahub),
) -> StrategyExecutionService:
    return datahub.cache.strategy_execution_service


def get_strategy_evidence_service(
    datahub: DataHub = Depends(get_datahub),
) -> StrategyEvidenceService:
    return datahub.cache.strategy_evidence_service


def get_strategy_automation_service(
    datahub: DataHub = Depends(get_datahub),
) -> StrategyAutomationService:
    return datahub.cache.strategy_automation_service


@router.get("/metrics", response_model=list[StrategyMetricDefinition])
async def strategy_metric_registry(
    response: Response,
    service: StrategyLabService = Depends(get_strategy_lab_service),
) -> list[StrategyMetricDefinition]:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(service.metrics)


@router.post("/compile", response_model=StrategyCompileResponse)
async def compile_strategy(
    payload: StrategyCompileRequest,
    service: StrategyLabService = Depends(get_strategy_lab_service),
) -> StrategyCompileResponse:
    return await run_sync_api_async(lambda: service.compile(payload))


@router.post("/parse", response_model=StrategyNaturalLanguageResponse)
async def parse_strategy(
    payload: StrategyNaturalLanguageRequest,
    service: StrategyLabService = Depends(get_strategy_lab_service),
) -> StrategyNaturalLanguageResponse:
    return await run_sync_api_async(lambda: service.parse_natural_language(payload))


@router.post("/strategies", response_model=StrategySpec, status_code=201)
async def create_strategy(
    payload: StrategySpecCreate,
    service: StrategyLabService = Depends(get_strategy_lab_service),
) -> StrategySpec:
    return await run_sync_api_async(lambda: service.create(payload))


@router.get("/strategies", response_model=StrategySpecPage)
async def list_strategies(
    response: Response,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_archived: bool = Query(False),
    service: StrategyLabService = Depends(get_strategy_lab_service),
) -> StrategySpecPage:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: service.list(
            page=page,
            page_size=page_size,
            include_archived=include_archived,
        )
    )


@router.get("/strategies/{strategy_id}", response_model=StrategySpec)
async def get_strategy(
    response: Response,
    strategy_id: int = Path(ge=1),
    revision: int | None = Query(default=None, ge=1),
    service: StrategyLabService = Depends(get_strategy_lab_service),
) -> StrategySpec:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.get(strategy_id, revision=revision))


@router.put("/strategies/{strategy_id}", response_model=StrategySpec)
async def update_strategy(
    payload: StrategySpecUpdate,
    strategy_id: int = Path(ge=1),
    service: StrategyLabService = Depends(get_strategy_lab_service),
) -> StrategySpec:
    return await run_sync_api_async(lambda: service.update(strategy_id, payload))


@router.post("/strategies/{strategy_id}/copy", response_model=StrategySpec, status_code=201)
async def copy_strategy(
    payload: StrategySpecCopyRequest,
    strategy_id: int = Path(ge=1),
    service: StrategyLabService = Depends(get_strategy_lab_service),
) -> StrategySpec:
    return await run_sync_api_async(lambda: service.copy(strategy_id, payload))


@router.post("/strategies/{strategy_id}/archive", response_model=StrategySpec)
async def archive_strategy(
    payload: StrategySpecArchiveRequest,
    strategy_id: int = Path(ge=1),
    service: StrategyLabService = Depends(get_strategy_lab_service),
) -> StrategySpec:
    return await run_sync_api_async(lambda: service.archive(strategy_id, payload))


@router.get("/strategies/{strategy_id}/versions", response_model=StrategyVersionPage)
async def strategy_versions(
    response: Response,
    strategy_id: int = Path(ge=1),
    service: StrategyLabService = Depends(get_strategy_lab_service),
) -> StrategyVersionPage:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.versions(strategy_id))


@router.get("/strategies/{strategy_id}/diff", response_model=StrategyVersionDiff)
async def strategy_version_diff(
    response: Response,
    strategy_id: int = Path(ge=1),
    left_revision: int = Query(ge=1),
    right_revision: int = Query(ge=1),
    service: StrategyLabService = Depends(get_strategy_lab_service),
) -> StrategyVersionDiff:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: service.diff(
            strategy_id,
            left_revision=left_revision,
            right_revision=right_revision,
        )
    )


@router.post("/executions", response_model=PortfolioDraft, status_code=201)
async def execute_strategy(
    payload: StrategyExecutionRequest,
    service: StrategyExecutionService = Depends(get_strategy_execution_service),
) -> PortfolioDraft:
    return await run_sync_api_async(lambda: service.execute(payload))


@router.get("/executions/compare", response_model=StrategyExecutionComparison)
async def compare_strategy_executions(
    response: Response,
    left_execution_id: int = Query(ge=1),
    right_execution_id: int = Query(ge=1),
    service: StrategyExecutionService = Depends(get_strategy_execution_service),
) -> StrategyExecutionComparison:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: service.compare(left_execution_id, right_execution_id)
    )


@router.get("/executions/{execution_id}", response_model=PortfolioDraft)
async def get_strategy_execution(
    response: Response,
    execution_id: int = Path(ge=1),
    service: StrategyExecutionService = Depends(get_strategy_execution_service),
) -> PortfolioDraft:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.draft(execution_id))


@router.get("/executions/{execution_id}/candidates", response_model=PortfolioCandidatePage)
async def strategy_execution_candidates(
    response: Response,
    execution_id: int = Path(ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status: PortfolioCandidateStatus | None = Query(None),
    sort_by: PortfolioCandidateSort = Query("utility_score"),
    descending: bool = Query(True),
    service: StrategyExecutionService = Depends(get_strategy_execution_service),
) -> PortfolioCandidatePage:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: service.candidates(
            execution_id,
            page=page,
            page_size=page_size,
            status=status,
            sort_by=sort_by,
            descending=descending,
        )
    )


@router.get("/strategies/{strategy_id}/executions", response_model=StrategyExecutionPage)
async def strategy_execution_history(
    response: Response,
    strategy_id: int = Path(ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    service: StrategyExecutionService = Depends(get_strategy_execution_service),
) -> StrategyExecutionPage:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: service.executions(
            strategy_id=strategy_id,
            page=page,
            page_size=page_size,
        )
    )


@router.get(
    "/strategies/{strategy_id}/evidence",
    response_model=StrategyEvidenceCenter | None,
)
async def strategy_evidence_center(
    response: Response,
    strategy_id: int = Path(ge=1),
    revision: int | None = Query(default=None, ge=1),
    mode: str = Query(default="official", pattern="^(official|intraday)$"),
    service: StrategyEvidenceService = Depends(get_strategy_evidence_service),
) -> StrategyEvidenceCenter | None:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: service.latest(strategy_id, revision=revision, mode=mode)
    )


@router.post(
    "/strategies/{strategy_id}/evidence/refresh",
    response_model=StrategyEvidenceCenter,
    status_code=201,
)
async def refresh_strategy_evidence_center(
    payload: StrategyEvidenceRefreshRequest,
    strategy_id: int = Path(ge=1),
    service: StrategyEvidenceService = Depends(get_strategy_evidence_service),
) -> StrategyEvidenceCenter:
    return await run_sync_api_async(
        lambda: service.refresh(
            strategy_id,
            revision=payload.revision,
            mode=payload.mode,
        )
    )


@router.post("/schedules", response_model=StrategySchedule, status_code=201)
async def create_strategy_schedule(
    payload: StrategyScheduleCreate,
    service: StrategyAutomationService = Depends(get_strategy_automation_service),
) -> StrategySchedule:
    return await run_sync_api_async(lambda: service.create_schedule(payload))


@router.get("/schedules", response_model=StrategySchedulePage)
async def list_strategy_schedules(
    response: Response,
    strategy_id: int | None = Query(default=None, ge=1),
    include_disabled: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    service: StrategyAutomationService = Depends(get_strategy_automation_service),
) -> StrategySchedulePage:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: service.schedules(
            strategy_id=strategy_id,
            include_disabled=include_disabled,
            page=page,
            page_size=page_size,
        )
    )


@router.patch("/schedules/{schedule_id}", response_model=StrategySchedule)
async def update_strategy_schedule(
    payload: StrategyScheduleUpdate,
    schedule_id: int = Path(ge=1),
    service: StrategyAutomationService = Depends(get_strategy_automation_service),
) -> StrategySchedule:
    return await run_sync_api_async(
        lambda: service.set_enabled(schedule_id, enabled=payload.enabled)
    )


@router.post("/automation/evaluate", response_model=StrategyAutomationRunSummary)
async def evaluate_strategy_automation(
    service: StrategyAutomationService = Depends(get_strategy_automation_service),
) -> StrategyAutomationRunSummary:
    return await run_sync_api_async(service.run_due)


@router.get("/alert-events", response_model=StrategyAlertEventPage)
async def strategy_alert_events(
    response: Response,
    strategy_id: int | None = Query(default=None, ge=1),
    schedule_id: int | None = Query(default=None, ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    service: StrategyAutomationService = Depends(get_strategy_automation_service),
) -> StrategyAlertEventPage:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(
        lambda: service.events(
            strategy_id=strategy_id,
            schedule_id=schedule_id,
            page=page,
            page_size=page_size,
        )
    )


@router.post(
    "/executions/{execution_id}/simulation-plan",
    response_model=StrategySimulationPlan,
    status_code=201,
)
async def create_strategy_simulation_plan(
    execution_id: int = Path(ge=1),
    service: StrategyAutomationService = Depends(get_strategy_automation_service),
) -> StrategySimulationPlan:
    return await run_sync_api_async(lambda: service.create_simulation_plan(execution_id))


@router.get(
    "/executions/{execution_id}/simulation-plan",
    response_model=StrategySimulationPlan | None,
)
async def get_strategy_simulation_plan(
    response: Response,
    execution_id: int = Path(ge=1),
    service: StrategyAutomationService = Depends(get_strategy_automation_service),
) -> StrategySimulationPlan | None:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: service.simulation_plan(execution_id))


__all__ = [
    "get_strategy_evidence_service",
    "get_strategy_automation_service",
    "get_strategy_execution_service",
    "get_strategy_lab_service",
    "router",
]
