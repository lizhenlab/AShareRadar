from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import get_datahub, get_local_data_import_previews
from app.api.errors import run_sync_api_async
from app.models.local_data import (
    LocalDataImportResult,
    RuntimeCleanupPreview,
    RuntimeCleanupResult,
    UserDataBundle,
)
from app.repositories.maintenance import USER_HISTORY_CLEANUP_SPECS
from app.services.datahub import DataHub
from app.services.local_data_import_guard import (
    LocalDataImportPreviewError,
    LocalDataImportPreviewRegistry,
    user_data_state_digest,
)
from app.services.runtime_backup import create_runtime_backup, verify_runtime_backup
from app.services.user_data_portability import export_user_data, import_user_data


router = APIRouter()
USER_HISTORY_RETENTION_TABLES = tuple(spec.table for spec in USER_HISTORY_CLEANUP_SPECS)


@router.post("/api/local-data/export", response_model=UserDataBundle)
async def export_local_user_data(
    response: Response,
    datahub: DataHub = Depends(get_datahub),
) -> UserDataBundle:
    response.headers["Cache-Control"] = "no-store"
    return await run_sync_api_async(lambda: export_user_data(datahub.cache.path))


@router.post("/api/local-data/import", response_model=LocalDataImportResult)
async def import_local_user_data(
    payload: UserDataBundle,
    response: Response,
    mode: Literal["merge", "replace"] = Query("merge"),
    dry_run: bool = Query(True),
    preview_token: str | None = Query(default=None, min_length=32, max_length=200),
    datahub: DataHub = Depends(get_datahub),
    previews: LocalDataImportPreviewRegistry = Depends(get_local_data_import_previews),
) -> LocalDataImportResult:
    response.headers["Cache-Control"] = "no-store"
    def execute() -> LocalDataImportResult:
        with datahub.cache.exclusive_local_data_operation():
            if dry_run:
                before = user_data_state_digest(datahub.cache.path)
                result = import_user_data(datahub.cache.path, payload, mode=mode, dry_run=True)
                after = user_data_state_digest(datahub.cache.path)
                if before != after:
                    raise LocalDataImportPreviewError("预览期间本地用户数据已变化，请重新预览")
                claim = previews.issue(datahub.cache.path, payload, mode, database_digest=after)
                return result.model_copy(
                    update={"preview_token": claim.token, "preview_expires_at": claim.expires_at}
                )

            previews.consume(preview_token, datahub.cache.path, payload, mode)
            backup_path = _create_verified_backup(datahub.cache.path)
            result = import_user_data(datahub.cache.path, payload, mode=mode, dry_run=False)
            return result.model_copy(update={"rollback_backup_path": backup_path})

    return await run_sync_api_async(execute)


@router.get("/api/local-data/cleanup-preview", response_model=RuntimeCleanupPreview)
async def preview_local_cleanup(datahub: DataHub = Depends(get_datahub)) -> RuntimeCleanupPreview:
    return await run_sync_api_async(lambda: _cleanup_preview(datahub.cache.preview_runtime_cleanup()))


@router.post("/api/local-data/cleanup", response_model=RuntimeCleanupResult)
async def cleanup_local_data(
    confirm: Literal["retention-cleanup"] = Query(...),
    datahub: DataHub = Depends(get_datahub),
) -> RuntimeCleanupResult:
    del confirm
    def cleanup() -> RuntimeCleanupResult:
        with datahub.cache.exclusive_local_data_operation():
            pending = _cleanup_preview(datahub.cache.preview_runtime_cleanup())
            backup_path = _create_verified_backup(datahub.cache.path) if pending.requires_user_backup else None
            removed = datahub.cache.cleanup_runtime_rows()
            preview = _cleanup_preview(removed)
            return RuntimeCleanupResult(
                **preview.model_dump(),
                committed=True,
                rollback_backup_path=backup_path,
            )

    return await run_sync_api_async(cleanup)


def _cleanup_preview(rows: dict[str, int]) -> RuntimeCleanupPreview:
    normalized = {name: max(0, int(count)) for name, count in rows.items()}
    user_history_rows = sum(normalized.get(name, 0) for name in USER_HISTORY_RETENTION_TABLES)
    return RuntimeCleanupPreview(
        tables=normalized,
        total_rows=sum(normalized.values()),
        user_history_rows=user_history_rows,
        requires_user_backup=user_history_rows > 0,
    )


def _create_verified_backup(path: Path) -> str:
    try:
        backup = create_runtime_backup(path)
        verify_runtime_backup(Path(backup.backup_path))
    except OSError as exc:
        raise RuntimeError("本地恢复备份创建失败，请检查数据目录权限和可用空间") from exc
    return backup.backup_path
