from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import cast


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from app.models.paper_trading import CostProfileName  # noqa: E402
from app.services.market_scan_probability_replay import (  # noqa: E402
    HistoricalReplayConfig,
    build_historical_replay_artifact,
    evaluate_market_scan_probability_replay,
    historical_replay_artifact_filename,
    write_historical_replay_artifact,
)


def main() -> int:
    args = _parser().parse_args()
    config = HistoricalReplayConfig(
        start_date=args.start_date,
        end_date=args.end_date,
        minimum_history_bars=args.minimum_history_bars,
        cost_profile=cast(CostProfileName, args.cost_profile),
        execution_notional=args.execution_notional,
        symbol_limit=args.symbol_limit,
        symbols=tuple(args.symbols or ()),
    )
    report = evaluate_market_scan_probability_replay(args.database, config=config)
    artifact = build_historical_replay_artifact(report)
    target = args.output_dir.expanduser().absolute() / historical_replay_artifact_filename(artifact)
    written = write_historical_replay_artifact(target, artifact, database_path=args.database)
    summary = _summary(report, artifact, written)
    if args.report is not None:
        _write_summary(args.report, summary, database=args.database, artifact=written)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读回填 historical_replay_v1 qfq OHLCV Shadow 概率研究源",
    )
    parser.add_argument("--database", type=Path, required=True, help="只读 SQLite 快照")
    parser.add_argument("--output-dir", type=Path, required=True, help="不可变 replay artifact 目录")
    parser.add_argument("--start-date", required=True, help="信号起始交易日 YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="信号结束交易日 YYYY-MM-DD")
    parser.add_argument("--minimum-history-bars", type=int, default=61)
    parser.add_argument("--cost-profile", choices=("base", "conservative", "stress"), default="base")
    parser.add_argument("--execution-notional", type=float, default=100_000.0)
    parser.add_argument("--symbol-limit", type=int, default=300, help="确定性分层样本上限，最大500")
    parser.add_argument("--symbol", action="append", dest="symbols", help="可重复；显式研究子集")
    parser.add_argument("--report", type=Path, help="可选机器摘要 JSON")
    return parser


def _summary(
    report: dict[str, object],
    artifact: dict[str, object],
    target: Path,
) -> dict[str, object]:
    quality = cast(dict[str, object], report["quality"])
    horizons = cast(dict[str, object], quality["horizons"])
    fit = cast(dict[str, object], report["probability_fit"])
    source = cast(dict[str, object], report["source"])
    integrity = cast(dict[str, object], artifact["integrity"])
    return {
        "schema_version": "market-scan-probability-historical-replay-summary-v1",
        "status": report["status"],
        "artifact": str(target),
        "artifact_bytes": target.stat().st_size,
        "integrity_digest": integrity["integrity_digest"],
        "cohort": report["cohort"],
        "official": False,
        "production_ranking_effect": "none",
        "record_count": quality["record_count"],
        "requested_signal_session_count": quality["requested_signal_session_count"],
        "selected_symbol_count": quality["selected_symbol_count"],
        "sampling_strategy": quality["sampling_strategy"],
        "h5_coverage": horizons["5"],
        "probability_fit": fit,
        "database_read_only": True,
        "database_immutable": source["sqlite_immutable"],
        "database_sidecar_policy": source["sqlite_sidecar_policy"],
        "database_query_only": True,
        "database_snapshot_transaction": True,
        "database_concurrent_external_change_detected": source[
            "database_concurrent_external_change_detected"
        ],
    }


def _write_summary(
    path: Path,
    summary: dict[str, object],
    *,
    database: Path,
    artifact: Path,
) -> None:
    target = path.expanduser().resolve()
    _reject_alias(target, database.expanduser().resolve(), "SQLite")
    _reject_alias(target, artifact.resolve(), "artifact")
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, prefix=f".{target.name}.", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _reject_alias(target: Path, source: Path, label: str) -> None:
    if target == source:
        raise ValueError(f"report 不能覆盖 {label}")
    if target.exists() and source.exists() and os.path.samefile(target, source):
        raise ValueError(f"report 不能硬链接 {label}")


if __name__ == "__main__":
    raise SystemExit(main())
