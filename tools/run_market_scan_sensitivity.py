"""Immutable offline execution sensitivity; never tune or promote a cell."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys
from typing import cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish, read_regular_file, sha256_hex  # noqa: E402
from app.services.market_scan_research_sensitivity import run_research_sensitivity  # noqa: E402
from app.services.market_scan_research_sensitivity_contract import admit_sensitivity_plan  # noqa: E402
from tools.run_market_scan_research import LoadedResearchBundle, load_research_bundle  # noqa: E402


MAX_REPORT_BYTES = 256 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="全市场固定成本/现金/参与率网格重放；不写数据库，不选择最优场景")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-digest", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--official-registry-digest")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def sensitivity_bundle_sessions(bundle: LoadedResearchBundle) -> tuple[str, ...]:
    signals = [batch.signal_date for batch in bundle.dataset.batches]
    if bundle.calendar.get("test_signal_dates") != signals:
        raise ValueError("bundle test signal dates must exactly match frozen batches")
    cutoff = date.fromisoformat(bundle.exploration_cutoff)
    if cutoff.isoformat() != bundle.exploration_cutoff or cutoff >= date.fromisoformat(signals[0]):
        raise ValueError("exploration cutoff must be an ISO date before the first evaluated signal")
    dates = cast(list[str], bundle.calendar["trading_dates"])
    if any(day not in dates for day in signals):
        raise ValueError("frozen signals must be inside the complete execution calendar")
    return tuple(day for day in dates if day >= signals[0])


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("output exists; retain it and use a new immutable path")
        raw_plan = decode_json_bytes(read_regular_file(args.plan, max_bytes=1024 * 1024))
        admit_sensitivity_plan(raw_plan, args.plan_digest)  # Pin the grid before opening any forward outcomes.
        bundle = load_research_bundle(args.bundle, official_registry_digest=args.official_registry_digest)
        report = run_research_sensitivity(bundle.dataset, sensitivity_bundle_sessions(bundle), raw_plan,
                                          expected_plan_digest=args.plan_digest, official_sessions=bundle.official_sessions,
                                          synthetic_rows=bundle.synthetic_rows)
        report["source_calendar"] = bundle.calendar
        report["exploration_cutoff"] = bundle.exploration_cutoff
        report["partition_usage"] = "fixed untrained scores; retained training/calibration metadata are not a new OOS certification"
        report.pop("digest")
        report["digest"] = sha256_hex(canonical_json_bytes(report))
        exclusive_atomic_publish(args.output, canonical_json_bytes(report), max_bytes=MAX_REPORT_BYTES)
        print(f"已保存固定情景报告：{args.output}", file=sys.stderr)
        return 0 if report["status"] == "complete" else 2
    except (ArtifactIOError, OSError, ValueError, RuntimeError, ArithmeticError) as exc:
        print(f"情景重放失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
