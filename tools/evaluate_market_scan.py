from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.market_scan_evaluation import (  # noqa: E402
    EvaluationConfig,
    evaluate_market_scan_rankings,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只读评估已发布全市场扫描榜单的后续收益与排名稳定性",
    )
    parser.add_argument("--database", type=Path, required=True, help="AShareRadar SQLite 数据库")
    parser.add_argument("--output", type=Path, help="JSON 报告输出路径；默认输出到 stdout")
    parser.add_argument("--mode", choices=("official", "intraday"))
    parser.add_argument("--run-id", type=int, action="append", dest="run_ids")
    parser.add_argument("--minimum-sample-size", type=int, default=30)
    parser.add_argument("--complete-day-coverage", type=float, default=0.95)
    args = parser.parse_args()
    report = evaluate_market_scan_rankings(
        args.database,
        config=EvaluationConfig(
            minimum_sample_size=args.minimum_sample_size,
            complete_day_coverage=args.complete_day_coverage,
        ),
        mode=args.mode,
        run_ids=args.run_ids,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is None:
        print(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
        print(f"报告已写入 {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
