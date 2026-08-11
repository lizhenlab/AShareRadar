from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.market_scan_future_range import (  # noqa: E402
    FutureRangeConfig,
    evaluate_market_scan_future_range,
)
from app.services.market_scan_future_range_artifact import (  # noqa: E402
    build_future_range_artifact,
    future_range_artifact_filename,
    replay_future_range_artifact,
    write_future_range_artifact,
)


def main() -> int:
    args = _parser().parse_args()
    database = args.database.expanduser().resolve()
    digest_before = _file_sha256(database)
    evaluation = evaluate_market_scan_future_range(
        database,
        config=FutureRangeConfig(
            minimum_sample_size=args.minimum_sample_size,
            minimum_session_count=args.minimum_session_count,
            complete_run_coverage=args.complete_run_coverage,
            bootstrap_samples=args.bootstrap_samples,
        ),
        run_ids=args.run_ids,
        probability_artifact_paths=args.probability_artifacts or (),
    )
    artifacts = _persist_reports(evaluation, args.output_dir, database)
    digest_after = _file_sha256(database)
    summary = _summary(evaluation, artifacts, digest_before, digest_after)
    if args.report is not None:
        artifact_paths = tuple(cast(Path, item["path"]) for item in artifacts)
        _write_json(args.report, summary, forbidden=(database, *artifact_paths))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读生成官方全市场扫描 D+1/D+2/D+3 固定交易日未来区间 artifact",
    )
    parser.add_argument("--database", type=Path, required=True, help="只读 AShareRadar SQLite 数据库")
    parser.add_argument("--output-dir", type=Path, required=True, help="不可变 artifact 输出目录")
    parser.add_argument("--report", type=Path, help="可选机器可读 CLI 摘要路径")
    parser.add_argument("--run-id", type=int, action="append", dest="run_ids")
    parser.add_argument(
        "--probability-artifact",
        type=Path,
        action="append",
        dest="probability_artifacts",
        help="可重复传入已持久化 OOS calibrated_shadow 概率 artifact",
    )
    parser.add_argument("--minimum-sample-size", type=int, default=30)
    parser.add_argument("--minimum-session-count", type=int, default=20)
    parser.add_argument("--complete-run-coverage", type=float, default=0.95)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    return parser


def _persist_reports(
    evaluation: dict[str, object],
    output_dir: Path,
    database: Path,
) -> list[dict[str, object]]:
    generated_at = str(evaluation["generated_at"])
    targets: list[dict[str, object]] = []
    for report in cast(list[dict[str, object]], evaluation["reports"]):
        run = cast(dict[str, object], report["run"])
        run_id = int(cast(int, run["run_id"]))
        artifact = build_future_range_artifact(report, generated_at=generated_at)
        filename = future_range_artifact_filename(run_id, artifact)
        target = write_future_range_artifact(
            output_dir.expanduser().resolve() / filename,
            artifact,
            database_path=database,
        )
        replay = replay_future_range_artifact(artifact)
        if replay != report:
            raise RuntimeError(f"未来区间 artifact 离线重放不一致：run={run_id}")
        integrity = cast(dict[str, object], artifact["integrity"])
        targets.append(
            {
                "run_id": run_id,
                "path": target,
                "artifact": str(target),
                "status": report["status"],
                "integrity_digest": integrity["integrity_digest"],
                "offline_replay_verified": True,
            }
        )
    return targets


def _summary(
    evaluation: dict[str, object],
    artifacts: list[dict[str, object]],
    digest_before: str,
    digest_after: str,
) -> dict[str, object]:
    serializable_artifacts = [
        {key: value for key, value in item.items() if key != "path"}
        for item in artifacts
    ]
    statuses = {str(item["status"]) for item in artifacts}
    return {
        "schema_version": "market-scan-future-range-evaluation-summary-v1",
        "status": "ok" if artifacts and statuses == {"ok"} else "insufficient_data",
        "artifact": serializable_artifacts[0]["artifact"] if len(serializable_artifacts) == 1 else None,
        "artifacts": serializable_artifacts,
        "artifact_count": len(artifacts),
        "session_offsets": [1, 2, 3],
        "center_proxy": "HLC3_proxy_not_VWAP",
        "database_read_only": True,
        "database_query_only": True,
        "database_sha256_before": digest_before,
        "database_sha256_after": digest_after,
        "database_bytes_unchanged": digest_before == digest_after,
        "database_concurrent_external_change_detected": digest_before != digest_after,
        "evaluation_transaction_snapshot": True,
        "production_ranking_effect": "none",
        "probability_production_effect": "none",
        "evaluation_status": evaluation["status"],
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object, *, forbidden: tuple[Path, ...]) -> Path:
    target = path.expanduser().resolve()
    for item in forbidden:
        resolved = item.expanduser().resolve()
        if target == resolved or (target.exists() and resolved.exists() and os.path.samefile(target, resolved)):
            raise ValueError("CLI 摘要不能覆盖 SQLite 或 future-range artifact")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
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
    return target


if __name__ == "__main__":
    raise SystemExit(main())
