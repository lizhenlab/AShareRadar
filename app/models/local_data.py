"""Strict contracts for local backup and user-data portability."""

from __future__ import annotations

from datetime import datetime
import math
from pathlib import PurePath
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


LOCAL_DATA_BUNDLE_KIND = "ashare-radar-user-data"
LOCAL_DATA_BUNDLE_VERSION = 1
RUNTIME_BACKUP_MANIFEST_VERSION = 1
CORE_USER_DATA_TABLES = (
    "watchlist",
    "alert_rule",
    "alert_event",
    "stock_note",
    "advice_history",
)
OPTIONAL_RESEARCH_USER_DATA_TABLES = (
    "advice_review_plan",
    "advice_review_result",
)
USER_DATA_TABLE_ALLOWLIST = frozenset((*CORE_USER_DATA_TABLES, *OPTIONAL_RESEARCH_USER_DATA_TABLES))
LocalDataImportMode = Literal["merge", "replace"]
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class StrictLocalDataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LocalDataTableBundle(StrictLocalDataModel):
    columns: list[str] = Field(min_length=1, max_length=256)
    column_types: dict[str, str] | None = Field(default=None, max_length=256)
    primary_key: list[str] = Field(default_factory=list, max_length=32)
    rows: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=1_000_000)

    @field_validator("columns", "primary_key")
    @classmethod
    def _validate_columns(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("column names must be unique")
        if any(_IDENTIFIER.fullmatch(value) is None for value in values):
            raise ValueError("invalid SQLite column name")
        return values

    @field_validator("column_types")
    @classmethod
    def _validate_column_types(cls, values: dict[str, str] | None) -> dict[str, str] | None:
        if values is None:
            return None
        if any(_IDENTIFIER.fullmatch(name) is None for name in values):
            raise ValueError("invalid SQLite column name in column_types")
        if any(len(value) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in value) for value in values.values()):
            raise ValueError("invalid SQLite declared type")
        return values

    @model_validator(mode="after")
    def _validate_rows(self) -> LocalDataTableBundle:
        column_set = set(self.columns)
        if self.column_types is not None and set(self.column_types) != column_set:
            raise ValueError("column_types must match the declared columns")
        if not set(self.primary_key).issubset(column_set):
            raise ValueError("primary key columns must be present in columns")
        for row in self.rows:
            if set(row) != column_set:
                raise ValueError("every row must contain exactly the declared columns")
            if any(not _portable_scalar(value) for value in row.values()):
                raise ValueError("rows may contain only finite JSON scalar values")
        return self


class UserDataBundle(StrictLocalDataModel):
    kind: Literal["ashare-radar-user-data"]
    version: Literal[1]
    exported_at: str
    source_schema_version: int = Field(ge=0)
    tables: dict[str, LocalDataTableBundle] = Field(min_length=1, max_length=64)
    row_counts: dict[str, int] = Field(min_length=1, max_length=64)

    @field_validator("exported_at")
    @classmethod
    def _validate_exported_at(cls, value: str) -> str:
        _parse_aware_timestamp(value)
        return value

    @model_validator(mode="after")
    def _validate_tables(self) -> UserDataBundle:
        names = set(self.tables)
        if not names.issubset(USER_DATA_TABLE_ALLOWLIST):
            raise ValueError("bundle contains a table outside the user-data allowlist")
        if names != set(self.row_counts):
            raise ValueError("row_counts must match the bundled tables")
        if any(self.row_counts[name] != len(table.rows) for name, table in self.tables.items()):
            raise ValueError("row_counts do not match bundled rows")
        return self


class LocalDataTableImportPreview(StrictLocalDataModel):
    incoming: int = Field(ge=0)
    inserted: int = Field(ge=0)
    updated: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    deleted: int = Field(ge=0)
    remapped: int = Field(default=0, ge=0)


class LocalDataImportResult(StrictLocalDataModel):
    bundle_version: Literal[1]
    mode: LocalDataImportMode
    dry_run: bool
    committed: bool
    conflict_strategy: Literal["remap_surrogate_ids_source_wins_on_stable_keys"]
    tables: dict[str, LocalDataTableImportPreview]
    totals: LocalDataTableImportPreview
    preview_token: str | None = Field(default=None, min_length=32, max_length=200)
    preview_expires_at: str | None = None
    rollback_backup_path: str | None = None

    @field_validator("preview_expires_at")
    @classmethod
    def _validate_preview_expiry(cls, value: str | None) -> str | None:
        if value is not None:
            _parse_aware_timestamp(value)
        return value


class RuntimeCleanupPreview(StrictLocalDataModel):
    tables: dict[str, int]
    total_rows: int = Field(ge=0)
    user_history_rows: int = Field(ge=0)
    requires_user_backup: bool


class RuntimeCleanupResult(RuntimeCleanupPreview):
    committed: Literal[True]
    rollback_backup_path: str | None = None


class RuntimeBackupManifest(StrictLocalDataModel):
    manifest_version: Literal[1]
    created_at: str
    source_path: str
    database_file: str
    database_size_bytes: int = Field(ge=0)
    schema_version: int = Field(ge=0)
    user_version: int = Field(ge=0)
    table_row_counts: dict[str, int]
    user_table_row_counts: dict[str, int]
    sha256: str
    integrity_check: Literal["ok"]

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: str) -> str:
        _parse_aware_timestamp(value)
        return value

    @field_validator("database_file")
    @classmethod
    def _validate_database_file(cls, value: str) -> str:
        if not value or PurePath(value).name != value or value in {".", ".."}:
            raise ValueError("database_file must be a plain file name")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _validate_counts(self) -> RuntimeBackupManifest:
        if any(value < 0 for value in self.table_row_counts.values()):
            raise ValueError("table row counts must be non-negative")
        if any(value < 0 for value in self.user_table_row_counts.values()):
            raise ValueError("user table row counts must be non-negative")
        if not set(self.user_table_row_counts).issubset(self.table_row_counts):
            raise ValueError("user table counts must be a subset of all table counts")
        return self


class RuntimeBackupResult(StrictLocalDataModel):
    backup_path: str
    database_path: str
    manifest_path: str
    manifest: RuntimeBackupManifest


class RuntimeBackupVerification(StrictLocalDataModel):
    ok: Literal[True]
    backup_path: str
    database_path: str
    manifest_path: str
    sha256: str
    integrity_check: Literal["ok"]
    manifest: RuntimeBackupManifest


class RuntimeRestoreResult(StrictLocalDataModel):
    restored: Literal[True]
    target_path: str
    backup_path: str
    rollback_backup_path: str | None = None
    integrity_check: Literal["ok"]


def _portable_scalar(value: JsonValue) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _parse_aware_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("timestamp must be valid ISO-8601 text") from None
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


__all__ = [
    "CORE_USER_DATA_TABLES",
    "LOCAL_DATA_BUNDLE_KIND",
    "LOCAL_DATA_BUNDLE_VERSION",
    "LocalDataImportMode",
    "LocalDataImportResult",
    "LocalDataTableBundle",
    "LocalDataTableImportPreview",
    "OPTIONAL_RESEARCH_USER_DATA_TABLES",
    "RUNTIME_BACKUP_MANIFEST_VERSION",
    "RuntimeBackupManifest",
    "RuntimeBackupResult",
    "RuntimeBackupVerification",
    "RuntimeCleanupPreview",
    "RuntimeCleanupResult",
    "RuntimeRestoreResult",
    "USER_DATA_TABLE_ALLOWLIST",
    "UserDataBundle",
]
