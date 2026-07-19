from __future__ import annotations

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
    LocalDataImportPreviewClaim,
    LocalDataImportPreviewError,
    LocalDataImportPreviewRegistry,
)
from app.services.runtime_backup import RuntimeBackupSession, runtime_backup_session
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
    return await run_sync_api_async(
        lambda: _run_local_user_data_import(
            payload,
            mode=mode,
            dry_run=dry_run,
            preview_token=preview_token,
            datahub=datahub,
            previews=previews,
        )
    )


def _run_local_user_data_import(
    payload: UserDataBundle,
    *,
    mode: Literal["merge", "replace"],
    dry_run: bool,
    preview_token: str | None,
    datahub: DataHub,
    previews: LocalDataImportPreviewRegistry,
) -> LocalDataImportResult:
    with datahub.cache.exclusive_local_data_operation():
        if dry_run:
            return _preview_local_user_data_import(payload, mode, datahub, previews)
        return _commit_local_user_data_import(payload, mode, preview_token, datahub, previews)


def _preview_local_user_data_import(
    payload: UserDataBundle,
    mode: Literal["merge", "replace"],
    datahub: DataHub,
    previews: LocalDataImportPreviewRegistry,
) -> LocalDataImportResult:
    claim: LocalDataImportPreviewClaim | None = None

    def issue_preview(database_digest: str, _result: LocalDataImportResult) -> None:
        nonlocal claim
        claim = previews.issue(
            datahub.cache.path,
            payload,
            mode,
            database_digest=database_digest,
        )

    result = import_user_data(
        datahub.cache.path,
        payload,
        mode=mode,
        dry_run=True,
        on_validated_state=issue_preview,
    )
    if claim is None:
        raise LocalDataImportPreviewError("导入预览未能建立服务端令牌，请重新预览")
    return result.model_copy(
        update={"preview_token": claim.token, "preview_expires_at": claim.expires_at}
    )


def _commit_local_user_data_import(
    payload: UserDataBundle,
    mode: Literal["merge", "replace"],
    preview_token: str | None,
    datahub: DataHub,
    previews: LocalDataImportPreviewRegistry,
) -> LocalDataImportResult:
    with runtime_backup_session(
        datahub.cache.path,
        max_backups=datahub.settings.max_runtime_backups,
    ) as backups:
        backup_path: str | None = None

        def validate_and_backup(database_digest: str, _result: LocalDataImportResult) -> None:
            nonlocal backup_path
            previews.consume(
                preview_token,
                datahub.cache.path,
                payload,
                mode,
                database_digest=database_digest,
            )
            backup_path = _create_verified_backup(backups)

        result = import_user_data(
            datahub.cache.path,
            payload,
            mode=mode,
            dry_run=False,
            on_validated_state=validate_and_backup,
        )
    if backup_path is None:
        raise RuntimeError("本地数据导入已提交，但恢复备份结果缺失")
    return result.model_copy(update={"rollback_backup_path": backup_path})


@router.get("/api/local-data/cleanup-preview", response_model=RuntimeCleanupPreview)
async def preview_local_cleanup(datahub: DataHub = Depends(get_datahub)) -> RuntimeCleanupPreview:
    def preview() -> RuntimeCleanupPreview:
        with datahub.cache.exclusive_local_data_operation() as operation:
            with operation.transaction():
                return _cleanup_preview(datahub.cache.preview_runtime_cleanup())

    return await run_sync_api_async(preview)


@router.post("/api/local-data/cleanup", response_model=RuntimeCleanupResult)
async def cleanup_local_data(
    confirm: Literal["retention-cleanup"] = Query(...),
    datahub: DataHub = Depends(get_datahub),
) -> RuntimeCleanupResult:
    del confirm

    def cleanup() -> RuntimeCleanupResult:
        with datahub.cache.exclusive_local_data_operation() as operation:
            with runtime_backup_session(
                datahub.cache.path,
                max_backups=datahub.settings.max_runtime_backups,
            ) as backups:
                with operation.transaction():
                    pending = _cleanup_preview(datahub.cache.preview_runtime_cleanup())
                    backup_path = (
                        _create_verified_backup(backups) if pending.requires_user_backup else None
                    )
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


def _create_verified_backup(session: RuntimeBackupSession) -> str:
    try:
        backup = session.create_verified_backup()
    except OSError as exc:
        raise RuntimeError("本地恢复备份创建失败，请检查数据目录权限和可用空间") from exc
    return backup.backup_path
