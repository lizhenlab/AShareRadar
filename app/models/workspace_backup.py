"""Strict, explicit scope and manifests for offline workspace archives."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

from app.models.local_data import RuntimeBackupManifest
from app.models.workspace_storage import WorkspaceStorageCategory


class BackupModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def backup_relative_path(value: str, *, allow_root: bool = False) -> str:
    if allow_root and value == ".":
        return value
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or str(path) != value or ".." in path.parts
            or "\\" in value or any(ord(char) < 32 for char in value) or value == "."):
        raise ValueError("archive paths must be canonical relative paths")
    return value


class WorkspaceBackupRoot(BackupModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    category: WorkspaceStorageCategory
    path: Path
    kind: Literal["file", "directory"]

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts or any(ord(char) < 32 for char in str(value)):
            raise ValueError("source roots require safe absolute paths")
        return value


class WorkspaceBackupDatabase(BackupModel):
    root: str
    relative_path: str
    role: Literal["primary", "auxiliary"]

    @field_validator("relative_path")
    @classmethod
    def _relative(cls, value: str) -> str:
        return backup_relative_path(value, allow_root=True)


class WorkspaceBackupLimits(BackupModel):
    max_files: int = Field(default=100_000, ge=1, le=1_000_000, strict=True)
    max_bytes: int = Field(default=50 * 1024**3, ge=1, le=1024**4, strict=True)
    max_file_bytes: int = Field(default=16 * 1024**3, ge=1, le=1024**4, strict=True)
    max_depth: int = Field(default=32, ge=0, le=128, strict=True)
    max_seconds: FiniteFloat = Field(default=900.0, gt=0, le=86_400, strict=True)


class WorkspaceBackupSpec(BackupModel):
    schema_version: Literal["workspace-backup-scope-v1"]
    roots: list[WorkspaceBackupRoot] = Field(min_length=1, max_length=64)
    databases: list[WorkspaceBackupDatabase] = Field(min_length=1, max_length=64)
    offline_writers_stopped: Literal[True]

    @field_validator("offline_writers_stopped", mode="before")
    @classmethod
    def _explicit_offline(cls, value: object) -> object:
        if value is not True:
            raise ValueError("offline_writers_stopped must explicitly be true")
        return value

    @model_validator(mode="after")
    def _scope(self) -> WorkspaceBackupSpec:
        roots = {root.name: root for root in self.roots}
        if len(roots) != len(self.roots) or sum(db.role == "primary" for db in self.databases) != 1:
            raise ValueError("roots must be unique and exactly one primary database is required")
        _require_disjoint_roots([root.path for root in self.roots])
        keys = {(db.root, db.relative_path) for db in self.databases}
        if len(keys) != len(self.databases):
            raise ValueError("database declarations must be unique")
        for database in self.databases:
            root = roots.get(database.root)
            if root is None or (root.kind == "file") != (database.relative_path == "."):
                raise ValueError("database declaration must identify a file inside its declared root")
        return self


def _require_disjoint_roots(paths: list[Path]) -> None:
    for index, path in enumerate(paths):
        if any(path == other or path in other.parents or other in path.parents for other in paths[index + 1:]):
            raise ValueError("source roots must not overlap")


class WorkspaceBackupMember(BackupModel):
    path: str
    size_bytes: int = Field(ge=0, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: int = Field(ge=0, le=0o777, strict=True)

    @field_validator("path")
    @classmethod
    def _relative(cls, value: str) -> str:
        return backup_relative_path(value)


class WorkspaceArchiveRoot(WorkspaceBackupRoot):
    archive_path: str

    @field_validator("archive_path")
    @classmethod
    def _relative(cls, value: str) -> str:
        return backup_relative_path(value)


class WorkspaceArchiveDatabase(WorkspaceBackupDatabase):
    archive_path: str
    manifest: RuntimeBackupManifest

    @field_validator("archive_path")
    @classmethod
    def _relative(cls, value: str) -> str:
        return backup_relative_path(value)


class WorkspaceBackupExclusion(BackupModel):
    root: str
    relative_path: str
    reason: str


class WorkspaceBackupManifest(BackupModel):
    schema_version: Literal["workspace-backup-manifest-v1"]
    operation: Literal["backup", "restore"]
    created_at: str
    coverage: Literal["explicit_roots_with_reported_exclusions"]
    consistency: Literal["offline_operator_attested_with_sqlite_write_leases"]
    cross_domain_consistency_verified: Literal[False]
    resume_eligible: Literal[False]
    source_backup_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    roots: list[WorkspaceArchiveRoot] = Field(min_length=1, max_length=64)
    databases: list[WorkspaceArchiveDatabase] = Field(min_length=1, max_length=64)
    directories: list[str] = Field(max_length=100_000)
    files: list[WorkspaceBackupMember] = Field(min_length=1, max_length=100_000)
    exclusions: list[WorkspaceBackupExclusion] = Field(max_length=100_000)
    domain_validation: dict[str, Any]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("directories")
    @classmethod
    def _directories(cls, values: list[str]) -> list[str]:
        return [backup_relative_path(value) for value in values]


__all__ = [
    "WorkspaceArchiveDatabase", "WorkspaceArchiveRoot", "WorkspaceBackupDatabase", "WorkspaceBackupExclusion",
    "WorkspaceBackupLimits", "WorkspaceBackupManifest", "WorkspaceBackupMember", "WorkspaceBackupRoot",
    "WorkspaceBackupSpec", "backup_relative_path",
]
