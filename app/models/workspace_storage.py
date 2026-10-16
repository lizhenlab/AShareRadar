"""Explicit scope and bounded, read-only workspace storage inspection results."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator


WorkspaceStorageCategory = Literal["database", "fuyao", "research", "prospective", "control", "runtime", "backup"]


class WorkspaceStorageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceStorageRoot(WorkspaceStorageModel):
    name: str = Field(min_length=1, max_length=120)
    category: WorkspaceStorageCategory
    path: Path

    @field_validator("name")
    @classmethod
    def _nonblank_name(cls, value: str) -> str:
        if value != value.strip() or not value.strip():
            raise ValueError("root name must be nonempty without surrounding whitespace")
        return value

    @field_validator("path")
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("root path must be absolute and must not contain '..'")
        return value


class WorkspaceStorageBudget(WorkspaceStorageModel):
    max_files: int = Field(default=100_000, ge=1, le=10_000_000, strict=True)
    max_directories: int = Field(default=10_000, ge=1, le=1_000_000, strict=True)
    max_entries: int = Field(default=200_000, ge=1, le=20_000_000, strict=True)
    max_depth: int = Field(default=32, ge=0, le=128, strict=True)
    max_seconds: FiniteFloat = Field(default=15.0, gt=0, le=3600, strict=True)
    max_issues: int = Field(default=200, ge=1, le=10_000, strict=True)


class StorageCounts(WorkspaceStorageModel):
    logical_bytes: int = 0
    unique_bytes: int = 0
    allocated_bytes: int = 0
    allocated_bytes_complete: bool = True
    file_count: int = 0
    unique_file_count: int = 0
    directory_count: int = 0
    duplicate_hardlink_count: int = 0
    overlap_count: int = 0
    symlink_count: int = 0
    excluded_count: int = 0
    error_count: int = 0


class WorkspaceStorageIssue(WorkspaceStorageModel):
    path: str
    reason: str
    detail: str


class WorkspaceStorageRootResult(WorkspaceStorageRoot):
    status: Literal["complete", "partial", "missing"] = "complete"
    counts: StorageCounts = Field(default_factory=StorageCounts)
    issues: list[WorkspaceStorageIssue] = Field(default_factory=list)
    issues_truncated: int = 0
    incomplete_reasons: list[str] = Field(default_factory=list)


class WorkspaceStorageReport(WorkspaceStorageModel):
    schema_version: Literal["workspace-storage-v1"] = "workspace-storage-v1"
    status: Literal["complete", "partial"]
    roots: list[WorkspaceStorageRootResult]
    totals: StorageCounts
    budget: WorkspaceStorageBudget
    entries_inspected: int
    files_inspected: int
    directories_inspected: int
    budget_exhausted: list[str]
    started_at: str
    finished_at: str
    elapsed_seconds: float
    exclusions: list[str]
    notes: list[str]


__all__ = [
    "StorageCounts", "WorkspaceStorageBudget", "WorkspaceStorageCategory", "WorkspaceStorageIssue",
    "WorkspaceStorageReport", "WorkspaceStorageRoot", "WorkspaceStorageRootResult",
]
