"""Inspect research readiness without opening the runtime database for writes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.market_scan_research_inputs import ResearchInputConfig, audit_research_inputs  # noqa: E402
from app.artifacts.io import exclusive_atomic_publish  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="只读盘点冻结研究输入，不读取收益或改写运行数据")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--as-of-date", required=True, help="预先固定的已完成交易日截止日 YYYY-MM-DD")
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--run-id", action="append", type=int, default=[], dest="run_ids")
    parser.add_argument("--max-runs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.resolve() == args.database.resolve():
        parser.error("audit output must not replace the input database")
    report = audit_research_inputs(args.database, ResearchInputConfig(
        as_of_date=args.as_of_date, horizon=args.horizon, run_ids=tuple(args.run_ids), max_runs=args.max_runs,
    ))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        exclusive_atomic_publish(args.output, rendered.encode("utf-8"), max_bytes=64 * 1024 * 1024)
    else:
        print(rendered, end="")
    return 0 if report["status"] == "audited" else 2


if __name__ == "__main__":
    raise SystemExit(main())
