"""Explicit offline entry points for frontier research diagnostics."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys
from typing import cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish, read_regular_file  # noqa: E402
from app.services.market_scan_probability_coherence import audit_probability_coherence  # noqa: E402
from app.services.market_scan_research_execution_audit import (  # noqa: E402
    ExecutionAuditDecision, audit_research_execution, capture_execution_audit_decision,
)
from app.services.market_scan_research_portfolio_models import ResearchPortfolioConfig  # noqa: E402
from app.services.market_scan_trial_registry_contract import registry_object  # noqa: E402
from tools.run_market_scan_research import load_research_bundle  # noqa: E402


MAX_BYTES = 256 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="全市场前沿研究离线审计；不修改评分、数据库或历史研究")
    commands = parser.add_subparsers(dest="command", required=True)
    execution = commands.add_parser("execution", help="同一分数的信号标签、成交衰减及共享账户比较")
    execution.add_argument("--bundle", type=Path, required=True)
    execution.add_argument("--official-registry-digest")
    execution.add_argument("--variant", choices=("production_v5", "without_continuous_trend", "smooth_turnover", "combined"), default="production_v5")
    execution.add_argument("--initial-cash", type=float, default=1_000_000)
    execution.add_argument("--top-n", type=int, default=100)
    execution.add_argument("--horizon", type=int, default=5)
    execution.add_argument("--cost-profile", choices=("base", "conservative", "stress"), default="base")
    execution.add_argument("--max-participation-rate", type=float, default=.01)
    execution.add_argument("--output", type=Path, required=True)
    coherence = commands.add_parser("coherence", help="相同条件下的多阈值概率自洽检查")
    coherence.add_argument("--input", type=Path, required=True)
    coherence.add_argument("--output", type=Path, required=True)
    holdings = commands.add_parser("holdings", help="实际持仓市值暴露；分类来源仍须独立认证")
    holdings.add_argument("--portfolio", type=Path, required=True)
    holdings.add_argument("--portfolio-digest", required=True)
    holdings.add_argument("--classifications", type=Path, required=True)
    holdings.add_argument("--classification-digest", required=True)
    holdings.add_argument("--output", type=Path, required=True)
    return parser


def read_frontier_object(path: Path) -> dict[str, object]:
    return registry_object(decode_json_bytes(read_regular_file(path, max_bytes=MAX_BYTES)), path.name)


def _execution(args: argparse.Namespace) -> dict[str, object]:
    decisions: list[ExecutionAuditDecision] = []
    bundle = load_research_bundle(
        args.bundle, official_registry_digest=args.official_registry_digest,
        snapshot_observer=lambda snapshot: decisions.append(capture_execution_audit_decision(snapshot)),
    )
    sessions = tuple(day for day in cast(list[str], bundle.calendar["trading_dates"]) if day >= bundle.dataset.batches[0].signal_date)
    config = ResearchPortfolioConfig(initial_cash=args.initial_cash, top_n=args.top_n, horizon=args.horizon,
                                    cost_profile=args.cost_profile, max_participation_rate=args.max_participation_rate)
    return audit_research_execution(bundle.dataset, decisions, sessions, variant=args.variant, config=config,
                                    official_sessions=bundle.official_sessions, synthetic_rows=bundle.synthetic_rows)


def _holdings(args: argparse.Namespace) -> dict[str, object]:
    from app.services.market_scan_research_holdings import audit_research_holdings_payload

    return audit_research_holdings_payload(
        read_frontier_object(args.portfolio), read_frontier_object(args.classifications),
        expected_portfolio_digest=args.portfolio_digest, expected_classification_digest=args.classification_digest,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("output already exists; use a new immutable path")
        if args.command == "execution":
            report = _execution(args)
        elif args.command == "holdings":
            report = _holdings(args)
        else:
            report = audit_probability_coherence(read_frontier_object(args.input))
        exclusive_atomic_publish(args.output, canonical_json_bytes(report), max_bytes=MAX_BYTES)
        print(f"已保存离线研究审计：{args.output}", file=sys.stderr)
        return 1 if report.get("status") == "incoherent" else 0
    except (ArtifactIOError, OSError, ValueError, RuntimeError) as exc:
        print(f"研究审计失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
