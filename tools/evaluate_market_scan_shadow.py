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
    evaluate_market_scan_shadow_comparison,
)
from app.services.market_scan_evaluation_compact import (  # noqa: E402
    compact_shadow_comparison_report,
)
from app.services.market_scan_shadow_scoring import SHADOW_SCORE_VARIANTS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只读比较生产全市场榜单与 Shadow Score v5.3/v5.4/v5.5；不会写回生产数据库",
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="JSON 输出路径；默认输出到 stdout")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="只保留证据中心所需聚合/排名差/门禁，省略巨型逐股研究记录",
    )
    parser.add_argument("--mode", choices=("official", "intraday", "preopen"))
    parser.add_argument("--run-id", type=int, action="append", dest="run_ids")
    parser.add_argument("--variant", action="append", choices=SHADOW_SCORE_VARIANTS, dest="variants")
    parser.add_argument("--minimum-sample-size", type=int, default=30)
    parser.add_argument("--minimum-session-count", type=int, default=20)
    parser.add_argument("--complete-day-coverage", type=float, default=0.95)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    parser.add_argument("--cost-profile", choices=("base", "conservative", "stress"), default="base")
    parser.add_argument("--execution-notional", type=float, default=100_000)
    parser.add_argument("--hysteresis-buffer-ratio", type=float, default=0.20)
    args = parser.parse_args()
    report = evaluate_market_scan_shadow_comparison(
        args.database,
        config=EvaluationConfig(
            minimum_sample_size=args.minimum_sample_size,
            minimum_session_count=args.minimum_session_count,
            complete_day_coverage=args.complete_day_coverage,
            bootstrap_samples=args.bootstrap_samples,
            cost_profile=args.cost_profile,
            execution_notional=args.execution_notional,
            hysteresis_buffer_ratio=args.hysteresis_buffer_ratio,
        ),
        mode=args.mode,
        run_ids=args.run_ids,
        variants=tuple(args.variants or SHADOW_SCORE_VARIANTS),
    )
    if args.compact:
        report = compact_shadow_comparison_report(report)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    if args.output is None:
        print(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
        print(f"影子评分比较报告已写入 {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
