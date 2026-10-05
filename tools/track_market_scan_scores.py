"""Build a read-only score-follow-up report from frozen scans and local daily bars."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from collections.abc import Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import (  # noqa: E402
    ArtifactIOError, canonical_json_bytes, exclusive_atomic_publish,
    path_has_only_trusted_aliases, sha256_hex,
)
from app.services.market_scan_evaluation import (  # noqa: E402
    EvaluationConfig, evaluate_market_scan_score_tracking,
)
from tools.score_tracking_report import render_score_tracking_html  # noqa: E402


MAX_REPORT_BYTES = 32 * 1024 * 1024


def publish_score_tracking_report(report: dict[str, object], directory: Path) -> tuple[Path, Path]:
    output = directory.expanduser().absolute()
    if {".git", ".codex", ".agents", ".venv"}.intersection(output.parts) or not path_has_only_trusted_aliases(output):
        raise ValueError("评分跟踪报告输出目录不安全")
    encoded = canonical_json_bytes(report)
    digest = sha256_hex(encoded)
    json_path = output / f"score-tracking-{digest}.json"
    html_path = output / f"score-tracking-{digest}.html"
    html = render_score_tracking_html(report).encode("utf-8")
    if max(len(encoded), len(html)) > MAX_REPORT_BYTES:
        raise ValueError("评分跟踪报告超过大小上限")
    exclusive_atomic_publish(json_path, encoded, max_bytes=MAX_REPORT_BYTES)
    exclusive_atomic_publish(html_path, html, max_bytes=MAX_REPORT_BYTES)
    return json_path, html_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跟踪冻结评分的后续表现；只读本地数据，不请求行情或更新模型")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--mode", choices=("official", "intraday", "preopen"), default="official")
    parser.add_argument("--run-id", type=int, action="append", dest="run_ids")
    parser.add_argument("--as-of", type=datetime.fromisoformat, help="观察截止时间，默认当前时间；无时区按上海时间")
    args = parser.parse_args(argv)
    try:
        report = evaluate_market_scan_score_tracking(
            args.database, mode=args.mode, run_ids=args.run_ids, as_of=args.as_of,
            config=EvaluationConfig(horizons=(1, 5, 20)),
        )
        json_path, html_path = publish_score_tracking_report(report, args.output_directory)
    except (ArtifactIOError, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({
        "json_path": str(json_path), "html_path": str(html_path),
        "evidence_status": report.get("status", "insufficient_data"),
        "production_ranking_effect": "none", "provider_requests": 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
