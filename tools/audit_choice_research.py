"""Verify Choice archives without SDK or input writes; optionally publish an immutable audit."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError  # noqa: E402
from app.services.choice_research_audit import audit_choice_research, publish_choice_audit  # noqa: E402
from app.services.choice_sdk import ChoiceError  # noqa: E402


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--history-dir", type=Path, default=ROOT / "data/research/choice_history_20260825")
    result.add_argument("--supplement-dir", type=Path, default=ROOT / "data/research/choice_supplement_20260825")
    result.add_argument("--universe-dir", type=Path, default=ROOT / "data/research/choice_daily_universe_20260825")
    result.add_argument("--control-dir", type=Path, default=ROOT / "data/research/choice_ingestion_control")
    result.add_argument("--audited-at", type=datetime.fromisoformat, help="Offline comparison timestamp with timezone; not live quota authorization")
    result.add_argument("--compact", action="store_true", help="Omit per-call account/ledger details; retain all priority anomalies")
    result.add_argument("--output-dir", type=Path, help="Explicitly publish a new content-addressed report outside all input directories")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        result = audit_choice_research(args.history_dir, args.supplement_dir, args.universe_dir, args.control_dir,
                                      audited_at=args.audited_at)
        if args.output_dir is not None:
            path = publish_choice_audit(result, args.output_dir, [args.history_dir, args.supplement_dir, args.universe_dir, args.control_dir])
            result["report_path"] = str(path)
        if args.compact:
            for key in ("account_snapshots", "reservation_candidates", "receipt_calls"):
                result["quota_reconciliation"].pop(key)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ChoiceError, ArtifactIOError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        # Native/raw exception text is deliberately not exported: no credential or
        # arbitrary response content should leak through a damaged audit input.
        print(json.dumps({"status": "audit_failed", "error_type": type(exc).__name__, "read_only": True,
                          "raw_replay_verified": False, "sdk_requests": 0, "reservations_released": 0}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
