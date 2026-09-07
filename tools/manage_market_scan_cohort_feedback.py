"""CLI for the independently versioned complete-date feedback experiment."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError, decode_json_bytes, exclusive_atomic_publish, read_regular_file  # noqa: E402
from app.services.market_scan_cohort_feedback import create_cohort_feedback_ledger, append_cohort_feedback_events, verify_cohort_feedback_ledger  # noqa: E402
from app.services.market_scan_cohort_feedback_calendar import capture_cohort_calendar  # noqa: E402
from app.services.market_scan_cohort_feedback_contracts import CohortFeedbackConfig  # noqa: E402
from app.services.market_scan_delayed_feedback_contracts import MAX_LEDGER_BYTES, feedback_json_bytes  # noqa: E402
from app.services.trading_calendar import next_trade_dates  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="完整日期群组反馈研究；冻结日历及每日全部预测，不迁移旧账本")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--config", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    append = commands.add_parser("append")
    append.add_argument("--events", type=Path, required=True)
    append.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    for command in (append, verify):
        command.add_argument("--ledger", type=Path, required=True)
        command.add_argument("--ledger-digest", required=True)
    return parser


def _read(path: Path) -> object:
    return decode_json_bytes(read_regular_file(path, max_bytes=MAX_LEDGER_BYTES))


def _create(config_payload: object) -> dict[str, object]:
    config = CohortFeedbackConfig.model_validate_json(feedback_json_bytes(config_payload), strict=True)
    recorded = utc_now()
    offset = config.event_definition.target_offset_sessions
    end = next_trade_dates(date.fromisoformat(config.signal_end), offset)[-1].isoformat() if offset else config.signal_end
    calendar = capture_cohort_calendar(config.signal_start, end, captured_at=recorded)
    return create_cohort_feedback_ledger(config.model_dump(mode="json"), calendar, recorded_at=recorded)


def _calculate(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "create":
        return _create(_read(args.config))
    if args.command == "verify":
        return verify_cohort_feedback_ledger(_read(args.ledger), expected_ledger_digest=args.ledger_digest)
    return append_cohort_feedback_events(_read(args.ledger), _read(args.events), expected_ledger_digest=args.ledger_digest, recorded_at=utc_now())


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command != "verify" and (args.output.exists() or args.output.is_symlink()):
            raise ValueError("output exists; use a new immutable path")
        ledger = _calculate(args)
        if args.command != "verify" and not exclusive_atomic_publish(args.output, feedback_json_bytes(ledger), max_bytes=MAX_LEDGER_BYTES):
            raise ValueError("output already published; overwrite forbidden")
        print(ledger["ledger_digest"])
        return 0
    except (ArtifactIOError, OSError, ValueError, RuntimeError) as error:
        print(f"完整日期群组账本失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
