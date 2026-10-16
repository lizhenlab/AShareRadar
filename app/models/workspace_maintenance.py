"""Administrator-owned scope and path-free results for the maintenance panel."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.workspace_storage import StorageCounts, WorkspaceStorageRoot


class MaintenanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BackupInspection(MaintenanceModel):
    name: str = Field(min_length=1, max_length=120)
    archive: Path
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    verification_receipt: Path | None = None

    @field_validator("archive", "verification_receipt")
    @classmethod
    def _absolute(cls, value: Path | None) -> Path | None:
        if value is not None and (not value.is_absolute() or ".." in value.parts):
            raise ValueError("maintenance paths must be absolute")
        return value


class RunnerInspection(MaintenanceModel):
    name: str = Field(min_length=1, max_length=120)
    root: Path
    plan_id: str
    control_root: Path
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_directory: Path
    runtime_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkspaceMaintenanceScope(MaintenanceModel):
    schema_version: Literal["workspace-maintenance-scope-v1"]
    storage_roots: list[WorkspaceStorageRoot] = Field(default_factory=list, max_length=64)
    backups: list[BackupInspection] = Field(default_factory=list, max_length=4)
    runners: list[RunnerInspection] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _unique_names(self) -> WorkspaceMaintenanceScope:
        for items in (self.storage_roots, self.backups, self.runners):
            if len({item.name for item in items}) != len(items):
                raise ValueError("maintenance names must be unique in each section")
        return self


class BackupMaintenanceStatus(MaintenanceModel):
    name: str
    status: Literal["manifest_checked", "unavailable"]
    created_at: str | None = None
    last_verified_at: str | None = None
    verification_record_status: Literal["not_supplied", "recorded", "undated", "invalid"] = "not_supplied"
    root_names: list[str] = Field(default_factory=list)
    file_count: int = 0
    size_bytes: int = 0
    excluded_count: int = 0
    current_payload_verified: Literal[False] = False
    resume_eligible: Literal[False] = False
    fault_code: str | None = None


class RunnerMaintenanceStatus(MaintenanceModel):
    name: str
    plan_id: str
    status: Literal["healthy", "fault", "unknown"]
    phase: str | None = None
    last_completed_at: str | None = None
    next_check_at: str | None = None
    fault_codes: list[str] = Field(default_factory=list)
    runtime_execution_verified: Literal[False] = False
    sample_admission_verified: Literal[False] = False


class MaintenanceStorageRoot(MaintenanceModel):
    name: str
    category: str
    status: str
    counts: StorageCounts
    incomplete_reasons: list[str] = Field(default_factory=list)


class MaintenanceStorageReport(MaintenanceModel):
    status: Literal["complete", "partial", "not_configured", "unavailable"]
    roots: list[MaintenanceStorageRoot] = Field(default_factory=list)
    totals: StorageCounts = Field(default_factory=StorageCounts)
    checked_at: str | None = None
    budget_exhausted: list[str] = Field(default_factory=list)


class WorkspaceMaintenanceReport(MaintenanceModel):
    schema_version: Literal["workspace-maintenance-status-v1"] = "workspace-maintenance-status-v1"
    status: Literal["configured", "not_configured", "unavailable"]
    checked_at: str
    scope_digest: str | None = None
    storage_root_count: int = 0
    backups: list[BackupMaintenanceStatus] = Field(default_factory=list)
    runners: list[RunnerMaintenanceStatus] = Field(default_factory=list)
    fault_code: str | None = None


__all__ = [
    "BackupInspection", "BackupMaintenanceStatus", "MaintenanceStorageReport", "MaintenanceStorageRoot",
    "RunnerInspection", "RunnerMaintenanceStatus", "WorkspaceMaintenanceReport", "WorkspaceMaintenanceScope",
]
