"""Create, verify and restore explicit offline archives into new directories only."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sqlite3
import sys
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError, decode_json_bytes, read_regular_file  # noqa: E402
from app.db.market_scan_artifact_lease import MarketScanArtifactLeaseError  # noqa: E402
from app.models.workspace_backup import WorkspaceBackupLimits, WorkspaceBackupSpec  # noqa: E402
from app.services.fuyao_dumps_validation import FuyaoDumpError  # noqa: E402
from app.services.runtime_backup import RuntimeBackupError  # noqa: E402
from app.services.workspace_backup_progress import BackupProgress  # noqa: E402
from app.services.workspace_backup import (  # noqa: E402
    create_workspace_backup, restore_workspace_backup, verify_workspace_backup,
)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError("invalid command line arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description="离线归档备份、验证与新目录恢复；不自动停止服务或启动恢复结果")
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("create", "verify", "restore"):
        command = commands.add_parser(operation)
        if operation == "create":
            command.add_argument("--scope", required=True, type=Path)
        else:
            command.add_argument("--archive", required=True, type=Path)
            command.add_argument("--expected-digest")
        if operation != "verify":
            command.add_argument("--destination", required=True, type=Path)
        command.add_argument("--progress", action="store_true", help="emit sanitized progress JSONL on stderr")
        command.add_argument("--progress-interval", type=float, default=2.0, help="cooperative heartbeat interval, 0.1–60 seconds")
        for name in WorkspaceBackupLimits.model_fields:
            command.add_argument("--" + name.replace("_", "-"), type=float if name == "max_seconds" else int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    operation = None
    progress = None
    try:
        args = _parser().parse_args(argv)
        operation = args.operation
        if args.progress:
            progress = BackupProgress(_print_progress, interval_seconds=args.progress_interval)
        limits = WorkspaceBackupLimits(**{name: getattr(args, name) for name in WorkspaceBackupLimits.model_fields
                                         if getattr(args, name) is not None})
        if args.operation == "create":
            spec = WorkspaceBackupSpec.model_validate(decode_json_bytes(read_regular_file(args.scope, max_bytes=64 * 1024)))
            result = create_workspace_backup(spec, args.destination, limits=limits, progress=progress)
        elif args.operation == "verify":
            result = verify_workspace_backup(args.archive, expected_digest=args.expected_digest, limits=limits, progress=progress)
        else:
            result = restore_workspace_backup(args.archive, args.destination, expected_digest=args.expected_digest, limits=limits, progress=progress)
    except (ArtifactIOError, OSError, ValueError, TypeError, KeyError, sqlite3.Error,
            RuntimeBackupError, MarketScanArtifactLeaseError, FuyaoDumpError) as exc:
        print(json.dumps({"status": "failed", "operation": operation, "archive_verified": False,
                          "resume_eligible": False, "error_type": type(exc).__name__,
                          **(progress.diagnostic() if progress is not None else {})}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _print_progress(event: dict[str, object]) -> None:
    print(json.dumps(event, ensure_ascii=True), file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
