"""Create, batch-append and verify immutable local research feedback ledgers."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError, decode_json_bytes, exclusive_atomic_publish, read_regular_file  # noqa: E402
from app.services.market_scan_delayed_feedback import append_feedback_events, create_feedback_ledger, verify_feedback_ledger  # noqa: E402
from app.services.market_scan_delayed_feedback_contracts import MAX_LEDGER_BYTES, feedback_json_bytes  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线延迟反馈研究账本：冻结预测、追加成熟标签、完整重放验证")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--config", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    append = commands.add_parser("append")
    append.add_argument("--ledger", type=Path, required=True)
    append.add_argument("--ledger-digest", required=True)
    append.add_argument("--events", type=Path, required=True, help="JSON event array; one atomic batch")
    append.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--ledger", type=Path, required=True)
    verify.add_argument("--ledger-digest", required=True)
    return parser


def _read(path: Path) -> object:
    return decode_json_bytes(read_regular_file(path, max_bytes=MAX_LEDGER_BYTES))


def _calculate(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "create":
        return create_feedback_ledger(_read(args.config), recorded_at=utc_now())
    ledger = _read(args.ledger)
    if args.command == "verify":
        return verify_feedback_ledger(ledger, expected_ledger_digest=args.ledger_digest)
    return append_feedback_events(ledger, _read(args.events), expected_ledger_digest=args.ledger_digest, recorded_at=utc_now())


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command != "verify" and (args.output.exists() or args.output.is_symlink()):
            raise ValueError("output already exists; use a new immutable path")
        ledger = _calculate(args)
        if args.command != "verify":
            if not exclusive_atomic_publish(args.output, feedback_json_bytes(ledger), max_bytes=MAX_LEDGER_BYTES):
                raise ValueError("output already exists; no overwrite allowed")
        print(ledger["ledger_digest"])
        return 0
    except (ArtifactIOError, OSError, ValueError, RuntimeError) as error:
        print(f"延迟反馈账本失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
