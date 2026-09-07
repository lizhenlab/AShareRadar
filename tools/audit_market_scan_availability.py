"""Audit a declared availability calendar offline without modifying runtime data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError, decode_json_bytes, exclusive_atomic_publish, read_regular_file  # noqa: E402
from app.services.market_scan_research_availability import audit_market_scan_availability  # noqa: E402
from app.services.market_scan_research_availability_contract import availability_plan_from_payload  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按预声明日历只读审计全市场数据可用性；不读取未来收益，不放宽研究准入")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True, help="完整交易日历、固定截止时刻和唯一批次选择规则的 JSON")
    parser.add_argument("--output", type=Path, required=True, help="运行库目录之外、尚不存在的独立 JSON 输出")
    return parser


def _validate_output(output: Path, database: Path, plan: Path) -> None:
    target = output.expanduser().resolve(strict=False)
    source = database.expanduser().resolve(strict=False)
    if target == plan.expanduser().resolve(strict=False) or target.is_relative_to(source.parent):
        raise ValueError("availability output must be separate from the plan and runtime database directory")
    if output.expanduser().exists() or output.expanduser().is_symlink():
        raise ValueError("availability output must be a new file")


def main() -> int:
    args = _parser().parse_args()
    try:
        _validate_output(args.output, args.database, args.plan)
        plan = availability_plan_from_payload(decode_json_bytes(read_regular_file(args.plan, max_bytes=1024 * 1024)))
        report = audit_market_scan_availability(args.database, plan)
        encoded = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        exclusive_atomic_publish(args.output, encoded, max_bytes=64 * 1024 * 1024)
    except (ArtifactIOError, OSError, ValueError) as exc:
        print(f"availability audit rejected: {exc}", file=sys.stderr)
        return 2
    return 0 if report["status"] == "audited" else 2


if __name__ == "__main__":
    raise SystemExit(main())
