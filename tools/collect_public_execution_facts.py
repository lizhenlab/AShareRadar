"""Collect free public facts once, or compare a fixed symbol set entirely offline."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sys
from typing import cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError  # noqa: E402
from app.services.public_execution_comparison import compare_public_execution_facts  # noqa: E402
from app.services.public_execution_store import (  # noqa: E402
    DEFAULT_PUBLIC_EXECUTION_ROOT, PUBLIC_EXECUTION_PROVIDERS, collect_public_execution_day, read_public_execution_sources,
)
from app.services.trading_calendar import latest_expected_daily_kline_date  # noqa: E402
from app.utils.clock import utc_now  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="免费公开执行事实；不读取API密钥、不购买权限、不交易")
    parser.add_argument("--root", type=Path, default=DEFAULT_PUBLIC_EXECUTION_ROOT)
    commands = parser.add_subparsers(dest="operation", required=True)
    collect = commands.add_parser("collect", help="采集后保留原始响应，已验证日期复用；每日最多12次逻辑采集")
    collect.add_argument("--date", help="缺省为最近完成交易日；当日17:45后可采集")
    collect.add_argument("--provider", choices=PUBLIC_EXECUTION_PROVIDERS, action="append")
    inspect = commands.add_parser("inspect", help="离线检查原始资料摘要与行数")
    inspect.add_argument("--date", required=True)
    compare = commands.add_parser("compare", help="固定股票集合逐项核验；不根据数据可得性删除股票")
    compare.add_argument("--date", required=True)
    compare.add_argument("--symbol", required=True, action="append")
    return parser


def _execute(args: argparse.Namespace) -> dict[str, object]:
    now = utc_now()
    day = args.date or latest_expected_daily_kline_date(now, allow_auto_refresh=False).isoformat()
    if args.operation == "collect":
        return collect_public_execution_day(args.root, day, providers=args.provider or PUBLIC_EXECUTION_PROVIDERS)
    pairs = [(symbol, day) for symbol in args.symbol] if args.operation == "compare" else None
    sources = read_public_execution_sources(args.root, [day], as_of=now, wanted_pairs=pairs)
    if pairs is not None:
        return compare_public_execution_facts(pairs, sources, as_of=now)
    return {"session_date": day, "as_of": now.isoformat(), "official_execution_admitted": False,
            "sources": [{"provider": provider, "status": row["status"], "digest": row["digest"],
                         "observed_at": row["observed_at"], "row_count": len(cast(list[object], row["rows"]))} for (provider, _day), row in sources.items()]}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _execute(args)
    except (ArtifactIOError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sources = result.get("sources", [])
    failed = isinstance(sources, list) and any(isinstance(row, dict) and row.get("status") == "invalid_response" for row in sources)
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
