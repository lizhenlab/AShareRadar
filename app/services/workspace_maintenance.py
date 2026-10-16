"""Read explicitly configured maintenance records without running archived code or scans."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, decode_json_bytes, read_regular_file, sha256_hex
from app.models.workspace_backup import WorkspaceBackupManifest
from app.models.workspace_maintenance import (
    BackupInspection, BackupMaintenanceStatus, MaintenanceStorageReport, MaintenanceStorageRoot,
    RunnerInspection, RunnerMaintenanceStatus, WorkspaceMaintenanceReport, WorkspaceMaintenanceScope,
)
from app.models.workspace_storage import WorkspaceStorageBudget
from app.services.market_scan_trial_registry_contract import registry_timestamp
from app.services.strategy_prospective_health import inspect_runner_health, prospective_runner_identity
from app.services.workspace_backup_manifest import MANIFEST_FILENAME, validate_workspace_layout
from app.services.workspace_storage import inspect_workspace_storage
from app.services.workspace_storage_policy import admitted_lexical_path
from app.utils.audit_time import audit_now_text
from app.utils.clock import utc_now


_READ_ERRORS = (ArtifactIOError, OSError, ValueError, TypeError, KeyError)


def maintenance_scope_path(database: Path) -> Path:
    return database.with_suffix(".maintenance.json")


def read_maintenance_scope(database: Path) -> tuple[WorkspaceMaintenanceScope, str]:
    data = read_regular_file(maintenance_scope_path(database), max_bytes=64 * 1024)
    scope = WorkspaceMaintenanceScope.model_validate(decode_json_bytes(data))
    return scope, sha256_hex(canonical_json_bytes(scope.model_dump(mode="json")))


def workspace_maintenance_status(database: Path) -> WorkspaceMaintenanceReport:
    checked_at = audit_now_text()
    path = maintenance_scope_path(database)
    try:
        if not path.exists() and not path.is_symlink():
            return WorkspaceMaintenanceReport(status="not_configured", checked_at=checked_at)
        scope, digest = read_maintenance_scope(database)
    except _READ_ERRORS:
        return WorkspaceMaintenanceReport(status="unavailable", checked_at=checked_at, fault_code="scope_invalid_or_unreadable")
    return WorkspaceMaintenanceReport(
        status="configured", checked_at=checked_at, scope_digest=digest, storage_root_count=len(scope.storage_roots),
        backups=[_backup_status(item) for item in scope.backups],
        runners=[_runner_status(database, item) for item in scope.runners],
    )


def workspace_maintenance_storage(database: Path, expected_scope_digest: str) -> MaintenanceStorageReport:
    try:
        scope, digest = read_maintenance_scope(database)
    except _READ_ERRORS as exc:
        raise ValueError("维护范围无法读取，请先刷新维护状态") from exc
    if digest != expected_scope_digest:
        raise ValueError("维护范围已变化，请先刷新维护状态")
    if not scope.storage_roots:
        return MaintenanceStorageReport(status="not_configured")
    budget = WorkspaceStorageBudget(max_files=10_000, max_entries=20_000, max_directories=2_000,
                                   max_seconds=2.0, max_issues=20)
    report = inspect_workspace_storage(scope.storage_roots, budget)
    try:
        current_digest = read_maintenance_scope(database)[1]
    except _READ_ERRORS as exc:
        raise ValueError("维护范围在检查期间无法读取，请刷新后重试") from exc
    if current_digest != digest:
        raise ValueError("维护范围在检查期间变化，请刷新后重试")
    return MaintenanceStorageReport(status=report.status, totals=report.totals, checked_at=report.finished_at,
        budget_exhausted=report.budget_exhausted, roots=[MaintenanceStorageRoot(
            name=item.name, category=item.category, status=item.status, counts=item.counts,
            incomplete_reasons=item.incomplete_reasons) for item in report.roots])


def _read_manifest(item: BackupInspection) -> WorkspaceBackupManifest:
    data = read_regular_file(item.archive / MANIFEST_FILENAME, max_bytes=2 * 1024 * 1024)
    manifest = WorkspaceBackupManifest.model_validate(decode_json_bytes(data))
    digest = sha256_hex(canonical_json_bytes(manifest.model_dump(mode="json", exclude={"digest"})))
    if digest != manifest.digest or digest != item.expected_digest:
        raise ValueError("maintenance archive identity mismatch")
    validate_workspace_layout(manifest)
    return manifest


def _backup_status(item: BackupInspection) -> BackupMaintenanceStatus:
    try:
        manifest = _read_manifest(item)
        result = BackupMaintenanceStatus(name=item.name, status="manifest_checked", created_at=manifest.created_at,
            root_names=[root.name for root in manifest.roots], file_count=len(manifest.files),
            size_bytes=sum(member.size_bytes for member in manifest.files), excluded_count=len(manifest.exclusions))
    except _READ_ERRORS:
        return BackupMaintenanceStatus(name=item.name, status="unavailable", fault_code="manifest_invalid_or_unreadable")
    if item.verification_receipt is not None:
        _read_verification_receipt(item, result)
    return result


def _read_verification_receipt(item: BackupInspection, result: BackupMaintenanceStatus) -> None:
    try:
        value = decode_json_bytes(read_regular_file(cast(Path, item.verification_receipt), max_bytes=256 * 1024))
        if not isinstance(value, dict) or value.get("schema_version") != "workspace-backup-result-v1":
            raise ValueError("invalid maintenance verification receipt")
        if (value.get("archive_verified") is not True or value.get("digest") != item.expected_digest
                or not _same_archive_path(value.get("archive_path"), item.archive)
                or value.get("operation") not in {"create", "verify", "restore"}):
            raise ValueError("maintenance verification receipt identity mismatch")
        if "verified_at" not in value:
            result.verification_record_status = "undated"
            return
        verified_at = registry_timestamp(value.get("verified_at"), "backup verification time")
        if verified_at > utc_now() or verified_at < registry_timestamp(result.created_at, "backup creation time"):
            raise ValueError("maintenance verification time is invalid")
        result.last_verified_at = verified_at.isoformat()
        result.verification_record_status = "recorded"
    except _READ_ERRORS:
        result.verification_record_status = "invalid"


def _same_archive_path(value: object, expected: Path) -> bool:
    if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
        return False
    admitted = admitted_lexical_path(Path(value))
    return admitted is not None and admitted == admitted_lexical_path(expected)


def _runner_status(database: Path, item: RunnerInspection) -> RunnerMaintenanceStatus:
    try:
        identity = prospective_runner_identity(database, item.root, item.plan_id, item.control_root,
            item.plan_digest, item.runtime_directory, item.runtime_digest)
        value = inspect_runner_health(identity)
        health = cast(dict[str, object], value.get("health", {}))
        return RunnerMaintenanceStatus.model_validate({"name": item.name, "plan_id": item.plan_id, "status": value["status"],
            "phase": health.get("status"), "last_completed_at": health.get("last_completed_at"),
            "next_check_at": health.get("next_check_at"), "fault_codes": value["fault_codes"]})
    except _READ_ERRORS:
        return RunnerMaintenanceStatus(name=item.name, plan_id=item.plan_id, status="unknown",
                                       fault_codes=["health_invalid_or_unreadable"])


__all__ = ["maintenance_scope_path", "read_maintenance_scope", "workspace_maintenance_status", "workspace_maintenance_storage"]
