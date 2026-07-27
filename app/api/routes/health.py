from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from app.api.container import AppContainer
from app.api.deps import get_app_settings, get_container, get_datahub
from app.config import Settings
from app.models.system import HealthProbe
from app.services.datahub import DataHub
from app.utils.audit_time import audit_now_text


router = APIRouter()


@router.get("/api/health")
async def health(
    response: Response,
    settings: Settings = Depends(get_app_settings),
) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok", "app": settings.app_name, "provider": settings.data_provider}


@router.get("/api/health/live", response_model=HealthProbe)
async def liveness(request: Request, response: Response) -> HealthProbe:
    settings: Settings = request.app.state.settings
    response.headers["Cache-Control"] = "no-store"
    return HealthProbe(
        status="ok",
        app=settings.app_name,
        checked_at=audit_now_text(),
        checks={"process": "up"},
    )


@router.get("/api/health/ready", response_model=HealthProbe)
async def readiness(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_app_settings),
    datahub: DataHub = Depends(get_datahub),
    container: AppContainer = Depends(get_container),
) -> HealthProbe:
    response.headers["Cache-Control"] = "no-store"
    accepting_requests = bool(getattr(request.app.state, "accepting_requests", False))
    if not accepting_requests:
        response.status_code = 503
        return HealthProbe(
            status="not_ready",
            app=settings.app_name,
            checked_at=audit_now_text(),
            checks={"container": "unavailable", "database": "not_checked"},
        )
    try:
        await asyncio.wait_for(run_in_threadpool(datahub.cache.readiness_check), timeout=1.0)
    except Exception:
        response.status_code = 503
        return HealthProbe(
            status="not_ready",
            app=settings.app_name,
            checked_at=audit_now_text(),
            checks={"container": "ready", "database": "unavailable", "runtime": _runtime_role(container)},
        )
    return HealthProbe(
        status="ready",
        app=settings.app_name,
        checked_at=audit_now_text(),
        checks={"container": "ready", "database": "ready", "runtime": _runtime_role(container)},
    )


def _runtime_role(container: AppContainer) -> str:
    coordinator = container.runtime_coordinator
    if coordinator is None:
        return "single"
    return "leader" if coordinator.leadership.is_leader else "standby"
