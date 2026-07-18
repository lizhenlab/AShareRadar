#!/usr/bin/env python3
"""Operate on the configured AShareRadar SQLite runtime database."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.services.runtime_backup import (  # noqa: E402
    create_runtime_backup,
    restore_runtime_backup,
    verify_runtime_backup,
)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = _run_command(args)
    except Exception as exc:
        print(f"runtime-data: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Back up, verify, or restore the SQLite database selected by Settings.cache_path."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="create a consistent SQLite snapshot and manifest")
    backup.add_argument(
        "--destination",
        "--output",
        type=Path,
        help="new backup directory; defaults beside the configured database under backups/",
    )

    verify = commands.add_parser("verify", help="verify manifest, SHA-256, row counts, and integrity")
    verify.add_argument("backup", type=Path, help="backup directory or its manifest.json")

    restore = commands.add_parser("restore", help="atomically restore the configured database")
    restore.add_argument("backup", type=Path, help="verified backup directory or its manifest.json")
    restore.add_argument(
        "--confirm-service-stopped",
        action="store_true",
        help="required acknowledgement; restore also refuses a held scheduler lock",
    )
    restore.add_argument(
        "--rollback-destination",
        type=Path,
        help="new directory for the automatic pre-restore rollback snapshot",
    )
    return parser.parse_args(argv)


def _run_command(args: argparse.Namespace):
    if args.command == "verify":
        return verify_runtime_backup(args.backup)
    settings = Settings()
    if args.command == "backup":
        return create_runtime_backup(settings.cache_path, args.destination)
    return restore_runtime_backup(
        args.backup,
        settings.cache_path,
        service_stopped=args.confirm_service_stopped,
        rollback_destination=args.rollback_destination,
    )


if __name__ == "__main__":
    raise SystemExit(main())
