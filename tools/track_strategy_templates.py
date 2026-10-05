"""Compare fixed templates using frozen scans and optional verified execution data."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import (  # noqa: E402
    ArtifactIOError, canonical_json_bytes, exclusive_atomic_publish,
    path_has_only_trusted_aliases, sha256_hex,
)
from app.services.strategy_template_tracking import evaluate_strategy_template_tracking  # noqa: E402
from app.services.market_scan_official_execution import VerifiedOfficialExecutionSession  # noqa: E402
from app.services.market_scan_official_execution_store import MarketScanOfficialExecutionStore  # noqa: E402
from tools.strategy_template_tracking_report import render_strategy_template_tracking_html  # noqa: E402


MAX_REPORT_BYTES = 32 * 1024 * 1024


def _official_execution_inputs(args: argparse.Namespace) -> tuple[VerifiedOfficialExecutionSession, ...]:
    values = (args.official_registry, args.official_registry_digest, args.official_raw_root, args.official_session_directory)
    if all(value is None for value in values):
        return ()
    if any(value is None for value in values):
        raise ValueError("official execution evidence requires all four pinned local source arguments")
    store = MarketScanOfficialExecutionStore(
        registry_path=args.official_registry, registry_digest=args.official_registry_digest,
        raw_file_root=args.official_raw_root, session_directory=args.official_session_directory,
    )
    sessions = store.sessions()
    if not sessions:
        raise ValueError("official execution evidence contains no verified sessions")
    return sessions


def publish_strategy_template_tracking_report(report: dict[str, object], directory: Path) -> tuple[Path, Path]:
    output = directory.expanduser().absolute()
    if {".git", ".codex", ".agents", ".venv"}.intersection(output.parts) or not path_has_only_trusted_aliases(output):
        raise ValueError("策略对照报告输出目录不安全")
    encoded = canonical_json_bytes(report)
    digest = sha256_hex(encoded)
    json_path = output / f"strategy-template-tracking-{digest}.json"
    html_path = output / f"strategy-template-tracking-{digest}.html"
    html = render_strategy_template_tracking_html(report).encode("utf-8")
    if max(len(encoded), len(html)) > MAX_REPORT_BYTES:
        raise ValueError("策略对照报告超过大小上限")
    exclusive_atomic_publish(json_path, encoded, max_bytes=MAX_REPORT_BYTES)
    exclusive_atomic_publish(html_path, html, max_bytes=MAX_REPORT_BYTES)
    return json_path, html_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定策略模板对照；只读本地数据，不请求行情、不拟合模型、不下单")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--horizon", type=int, choices=(1, 5, 10, 20), default=10)
    parser.add_argument("--notional-cash-cny", type=float, default=1_000_000)
    parser.add_argument("--run-id", type=int, action="append", dest="run_ids")
    parser.add_argument("--non-overlapping-signals", action="store_true", help="按每合同首日固定每H+1交易日读取原始批次；不随结果补位")
    parser.add_argument("--as-of", type=datetime.fromisoformat, help="观察截止时间，默认当前时间；无时区按上海时间")
    parser.add_argument("--official-registry", type=Path, help="可选：官方成交证据注册表；须与摘要和两目录同时提供")
    parser.add_argument("--official-registry-digest", help="独立确认的注册表SHA-256")
    parser.add_argument("--official-raw-root", type=Path, help="注册的原始交付文件目录")
    parser.add_argument("--official-session-directory", type=Path, help="已封存的逐交易日执行证据目录")
    args = parser.parse_args(argv)
    try:
        report = evaluate_strategy_template_tracking(
            args.database, as_of=args.as_of, horizon=args.horizon,
            notional_cash_cny=args.notional_cash_cny, run_ids=args.run_ids,
            official_sessions=_official_execution_inputs(args),
            non_overlapping_signals=args.non_overlapping_signals,
        )
        json_path, html_path = publish_strategy_template_tracking_report(report, args.output_directory)
    except (ArtifactIOError, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2
    selection = report.get("strategy_selection")
    selection = selection if isinstance(selection, dict) else {}
    print(json.dumps({
        "json_path": str(json_path), "html_path": str(html_path),
        "evidence_status": report.get("status", "insufficient_data"),
        "selection_status": selection.get("status", "not_evaluated"),
        "adoptable_template_id": selection.get("adoptable_template_id"),
        "production_ranking_effect": "none", "provider_requests": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
