from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_app_settings
from app.config import Settings


router = APIRouter()


@router.get("/api/health")
async def health(settings: Settings = Depends(get_app_settings)) -> dict[str, str]:
    return {"status": "ok", "app": settings.app_name, "provider": settings.data_provider}
