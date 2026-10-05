"""Predeclare and capture future strategy inputs; never reconstruct a missed signal."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError  # noqa: E402
from app.services.public_execution_store import DEFAULT_PUBLIC_EXECUTION_ROOT  # noqa: E402
from app.services.market_scan_official_execution import VerifiedOfficialExecutionSession  # noqa: E402
from app.services.market_scan_official_execution_store import MarketScanOfficialExecutionStore  # noqa: E402
from app.services.strategy_prospective_collection import collect_strategy_prospective_inputs  # noqa: E402
from app.services.strategy_prospective_outcomes import (  # noqa: E402
    collect_prospective_execution_evidence, evaluate_prospective_execution,
)
from app.services.strategy_prospective_plan import (  # noqa: E402
    create_strategy_prospective_plan, strategy_prospective_status,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="三模板独立前瞻采集；原始信号按时留档，缺失不回填，不交易")
    parser.add_argument("--root", type=Path, default=ROOT / "data/research/strategy_prospective")
    parser.add_argument("--plan-id", required=True)
    commands = parser.add_subparsers(dest="operation", required=True)
    init = commands.add_parser("init", help="在未来采集日之前冻结模板、日历、代码及统计合同")
    init.add_argument("--start-date", required=True)
    init.add_argument("--end-date", required=True)
    init.add_argument("--horizon", type=int, choices=(1, 5, 10, 20), default=10)
    init.add_argument("--notional-cash-cny", type=float, default=1_000_000)
    init.add_argument("--cutoff-local", default="20:00:00")
    capture = commands.add_parser("capture", help="仅从本地冻结扫描捕获到期信号；不读未来行情、不启动扫描")
    capture.add_argument("--database", type=Path, default=ROOT / "data/ashare_radar.sqlite3")
    commands.add_parser("status", help="校验计划与收据链，并报告成熟与缺口；不消费供应商额度")
    evidence = commands.add_parser("evidence", help="保留已选股票所需的原始研究资料，列出正式执行证据缺口")
    evidence.add_argument("--history-root", type=Path, default=ROOT / "data/ashare_radar.fuyao/history")
    evidence.add_argument("--public-root", type=Path, default=DEFAULT_PUBLIC_EXECUTION_ROOT)
    evaluate = commands.add_parser("evaluate", help="仅为已按时冻结的篮子追加执行结果，不重新选股")
    evaluate.add_argument("--official-registry", type=Path)
    evaluate.add_argument("--official-registry-digest")
    evaluate.add_argument("--official-raw-root", type=Path)
    evaluate.add_argument("--official-session-directory", type=Path)
    return parser


def _official_sessions(args: argparse.Namespace) -> tuple[VerifiedOfficialExecutionSession, ...]:
    parts = (args.official_registry, args.official_registry_digest, args.official_raw_root, args.official_session_directory)
    if all(value is None for value in parts):
        return ()
    if any(value is None for value in parts):
        raise ValueError("all four official execution source arguments are required")
    store = MarketScanOfficialExecutionStore(registry_path=args.official_registry, registry_digest=args.official_registry_digest,
                                             raw_file_root=args.official_raw_root, session_directory=args.official_session_directory)
    sessions = store.sessions()
    if not sessions:
        raise ValueError("explicit official execution store contains no verified sessions")
    return sessions


def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.operation == "init":
        create_strategy_prospective_plan(args.root, args.plan_id, start_date=args.start_date, end_date=args.end_date,
                                        horizon=args.horizon, notional_cash_cny=args.notional_cash_cny, cutoff_local=args.cutoff_local)
        return strategy_prospective_status(args.root, args.plan_id)
    if args.operation == "capture":
        return collect_strategy_prospective_inputs(args.database, args.root, args.plan_id)
    if args.operation == "evidence":
        return collect_prospective_execution_evidence(args.root, args.plan_id, args.history_root, public_root=args.public_root)
    if args.operation == "evaluate":
        return evaluate_prospective_execution(args.root, args.plan_id, official_sessions=_official_sessions(args))
    return strategy_prospective_status(args.root, args.plan_id)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _execute(args)
    except (ArtifactIOError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "failed", "operation": args.operation, "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
