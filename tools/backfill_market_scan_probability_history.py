from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
from typing import cast


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from app.services.market_scan_probability_history import (  # noqa: E402
    ProbabilityHistoryBuildResult,
    ProbabilityHistoryConfig,
    ProbabilityHistoryError,
    backfill_market_scan_probability_history,
)


def main() -> int:
    args = _parser().parse_args()
    try:
        _validate_timeout(args.timeout)
        result = asyncio.run(
            backfill_market_scan_probability_history(
                args.source_database,
                args.target_database,
                args.output_dir,
                config=ProbabilityHistoryConfig(
                    symbol_limit=args.symbol_limit,
                    symbols=tuple(args.symbols or ()),
                ),
                provider_timeout_seconds=args.timeout,
            ),
        )
    except (ProbabilityHistoryError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": "market-scan-probability-history-cli-summary-v1",
                    "status": "failed",
                    "error": str(exc),
                    "target_published": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(_summary(result), ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 Tencent qfq 日K构建独立、不可变、可直接回放的历史研究 SQLite",
    )
    parser.add_argument(
        "--source-database",
        type=Path,
        required=True,
        help="runtime_data.py backup 生成的 runtime.sqlite3；必须保留相邻 manifest.json",
    )
    parser.add_argument("--target-database", type=Path, required=True, help="全新的独立研究 SQLite")
    parser.add_argument("--output-dir", type=Path, required=True, help="内容寻址 manifest 目录")
    parser.add_argument("--symbol-limit", type=int, default=90, help="均衡样本数，默认90、最大120")
    parser.add_argument("--symbol", action="append", dest="symbols", help="可重复；仍须满足60/每市场20门槛")
    parser.add_argument("--timeout", type=float, default=15.0, help="Tencent 单次请求超时秒数")
    return parser


def _validate_timeout(value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout 必须是正有限数")


def _summary(result: ProbabilityHistoryBuildResult) -> dict[str, object]:
    payload = cast(dict[str, object], result.manifest["payload"])
    database = cast(dict[str, object], payload["database"])
    quality = cast(dict[str, object], payload["quality"])
    integrity = cast(dict[str, object], result.manifest["integrity"])
    replay = cast(dict[str, object], payload["replay_input"])
    source = cast(dict[str, object], payload["source"])
    runtime_backup = cast(dict[str, object], source["runtime_backup"])
    return {
        "schema_version": "market-scan-probability-history-cli-summary-v1",
        "status": "ready",
        "official": False,
        "production_ranking_effect": "none",
        "database": str(result.database_path),
        "manifest": str(result.manifest_path),
        "manifest_digest": integrity["integrity_digest"],
        "source_backup_manifest": runtime_backup["manifest_file"],
        "source_backup_sha256": runtime_backup["verified_sha256"],
        "source_backup_verified_before_and_after_fetch": runtime_backup[
            "verified_before_and_after_fetch"
        ],
        "selected_symbol_count": quality["accepted_symbol_count"],
        "selected_market_counts": quality["accepted_market_counts"],
        "bar_start": database["bar_start"],
        "bar_end": database["bar_end"],
        "bars_per_symbol": database["bars_per_symbol"],
        "bar_coverage": quality["bar_coverage"],
        "replay_input": replay,
        "replay_cli": [
            ".venv/bin/python",
            "tools/backfill_market_scan_probability_replay.py",
            "--database",
            str(result.database_path),
            "--output-dir",
            "<REPLAY_OUTPUT_DIR>",
            "--start-date",
            str(replay["start_date"]),
            "--end-date",
            str(replay["end_date"]),
            "--symbol-limit",
            str(quality["accepted_symbol_count"]),
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
