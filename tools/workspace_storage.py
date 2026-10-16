"""Inspect explicitly declared local storage and preview backup scope without writing data."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError, decode_json_bytes, read_regular_file  # noqa: E402
from app.models.workspace_storage import WorkspaceStorageBudget, WorkspaceStorageRoot  # noqa: E402
from app.services.workspace_storage import inspect_workspace_storage  # noqa: E402


SCOPE_MAX_BYTES = 64 * 1024
_BUDGET_FIELDS = ("max_files", "max_directories", "max_entries", "max_depth", "max_seconds", "max_issues")


class WorkspaceStorageScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["workspace-storage-scope-v1"]
    roots: list[WorkspaceStorageRoot] = Field(min_length=1, max_length=64)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读容量统计与备份范围预览；不创建、恢复或删除备份")
    parser.add_argument("operation", choices=("inventory", "preview"))
    parser.add_argument("--scope", required=True, type=Path, help="显式根清单JSON，最多64KiB；根路径必须为绝对路径")
    for name in _BUDGET_FIELDS:
        parser.add_argument("--" + name.replace("_", "-"), type=float if name == "max_seconds" else int)
    return parser


def _load_scope(path: Path) -> WorkspaceStorageScope:
    return WorkspaceStorageScope.model_validate(decode_json_bytes(read_regular_file(path, max_bytes=SCOPE_MAX_BYTES)))


def _backup_preview(inventory: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "workspace-backup-preview-v1", "operation": "preview", "read_only": True,
        "status": inventory["status"], "backup_created": False, "restore_verified": False,
        "coverage_basis": "explicit_roots_only_not_discovered_database_references",
        "consistency": "not_established_live_metadata_observation",
        "pending_backup_requirements": [
            "quiesce_application_runner_and_sync_writers",
            "create_sqlite_snapshots_with_backup_api_for_each_database",
            "validate_referenced_artifacts_and_external_runtime_dependencies",
            "verify_manifest_and_restore_in_new_empty_directory",
        ],
        "inventory": inventory,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        scope = _load_scope(args.scope)
        budget = WorkspaceStorageBudget(**{name: getattr(args, name) for name in _BUDGET_FIELDS if getattr(args, name) is not None})
        inventory = inspect_workspace_storage(scope.roots, budget).model_dump(mode="json")
        result = _backup_preview(inventory) if args.operation == "preview" else {
            **inventory, "operation": "inventory", "read_only": True,
        }
    except (ArtifactIOError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "failed", "operation": args.operation, "read_only": True,
                          "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if inventory["status"] == "complete" else 3


if __name__ == "__main__":
    raise SystemExit(main())
