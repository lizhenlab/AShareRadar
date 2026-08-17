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
from app.db.market_scan_artifact_lease import (  # noqa: E402
    verified_market_scan_artifact_batch_publication,
)


def main() -> int:
    args = _parser().parse_args()
    _require_managed_output_database(args.database, args.output_dir, args.report)
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
    planned = _plan_reports(evaluation, args.output_dir)
    managed_summary = database.parent / "research/market-scan-future-range-summary.json"
    batch_paths = [path for _run_id, _report, path, _artifact in planned]
    report_is_managed = _managed_summary_target(args.report, managed_summary)
    if report_is_managed:
        batch_paths.append(managed_summary)
    with verified_market_scan_artifact_batch_publication(
        database,
        tuple(batch_paths),
        tuple(run_id for run_id, _report, _path, _artifact in planned),
        managed_directory="research/market_scan_future_range",
        managed_files=("research/market-scan-future-range-summary.json",),
    ):
        artifacts = _persist_planned_reports(planned, database)
        digest_after = _file_sha256(database)
        summary = _summary(evaluation, artifacts, digest_before, digest_after)
        if report_is_managed:
            artifact_paths = tuple(cast(Path, item["path"]) for item in artifacts)
            _write_json(args.report, summary, forbidden=(database, *artifact_paths))
    if args.report is not None and not report_is_managed:
        artifact_paths = tuple(cast(Path, item["path"]) for item in artifacts)
        _write_json(args.report, summary, forbidden=(database, *artifact_paths))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


def _managed_summary_target(report: Path | None, managed: Path) -> bool:
    if report is None:
        return False
    lexical = report.expanduser().absolute()
    resolved = lexical.resolve(strict=False)
    managed_resolved = managed.resolve(strict=False)
    if resolved == managed_resolved and lexical != managed:
        raise ValueError("受管 future-range summary 不能通过路径别名发布")
    return lexical == managed


def _require_managed_output_database(
    database: Path,
    output: Path,
    report: Path | None,
) -> None:
    managed = ROOT / "data" / "research" / "market_scan_future_range"
    target = output.expanduser().absolute()
    if target.resolve(strict=False) == managed.resolve(strict=False) and target != managed:
        raise ValueError("受管 future-range 输出不能通过路径别名访问")
    if target == managed and database.expanduser().absolute() != ROOT / "data" / "ashare_radar.sqlite3":
        raise ValueError("受管 future-range 输出必须绑定同一 data 根运行库")
    fixed_summary = ROOT / "data" / "research" / "market-scan-future-range-summary.json"
    if report is not None:
        report_target = report.expanduser().absolute()
        if report_target.resolve(strict=False) == fixed_summary.resolve(strict=False) and report_target != fixed_summary:
            raise ValueError("受管 future-range summary 不能通过路径别名访问")
        if report_target == fixed_summary and database.expanduser().absolute() != ROOT / "data" / "ashare_radar.sqlite3":
            raise ValueError("受管 future-range summary 必须绑定正式运行库")


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


def _plan_reports(
    evaluation: dict[str, object],
    output_dir: Path,
) -> list[tuple[int, dict[str, object], Path, dict[str, object]]]:
    generated_at = str(evaluation["generated_at"])
    planned: list[tuple[int, dict[str, object], Path, dict[str, object]]] = []
    for report in cast(list[dict[str, object]], evaluation["reports"]):
        run = cast(dict[str, object], report["run"])
        run_id = int(cast(int, run["run_id"]))
        artifact = build_future_range_artifact(report, generated_at=generated_at)
        path = output_dir.expanduser().absolute() / future_range_artifact_filename(
            run_id,
            artifact,
        )
        planned.append((run_id, report, path, artifact))
    return planned


def _persist_planned_reports(
    planned: list[tuple[int, dict[str, object], Path, dict[str, object]]],
    database: Path,
) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    for run_id, report, path, artifact in planned:
        target = write_future_range_artifact(path, artifact, database_path=database)
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
    serializable_artifacts = [{key: value for key, value in item.items() if key != "path"} for item in artifacts]
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
    target = path.expanduser().absolute()
    fixed = ROOT / "data/research/market-scan-future-range-summary.json"
    if target != fixed:
        _reject_project_managed_report(target)
    for item in forbidden:
        resolved = item.expanduser().resolve()
        if target == resolved or (target.exists() and resolved.exists() and os.path.samefile(target, resolved)):
            raise ValueError("CLI 摘要不能覆盖 SQLite 或 future-range artifact")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    parent_before = _prepare_summary_target(target)
    _replace_summary_bytes(target, encoded, parent_before)
    return target


def _reject_project_managed_report(target: Path) -> None:
    managed = (
        ROOT / "data/market-scan-probability",
        ROOT / "data/research/market_scan_probability_source",
        ROOT / "data/research/market_scan_probability_outcomes",
        ROOT / "data/research/market_scan_probability_fit",
        ROOT / "data/research/market_scan_future_range",
        ROOT / "data/research/individual_probability",
        ROOT / "docs/research/artifacts",
    )
    resolved = target.resolve(strict=False)
    if any(directory.resolve(strict=False) in resolved.parents for directory in managed):
        raise ValueError("CLI 摘要不能覆盖项目受管 artifact")


def _prepare_summary_target(target: Path) -> os.stat_result:
    target.parent.mkdir(parents=True, exist_ok=True)
    parent = target.parent.lstat()
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise ValueError("CLI 摘要父目录必须是真实目录")
    if target.exists() or target.is_symlink():
        facts = target.lstat()
        if not target.is_file() or target.is_symlink() or facts.st_nlink != 1:
            raise ValueError("CLI 摘要目标必须是无别名普通文件")
    return parent


def _replace_summary_bytes(
    target: Path,
    encoded: str,
    parent_before: os.stat_result,
) -> None:
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
        parent_after = target.parent.lstat()
        if (parent_before.st_dev, parent_before.st_ino) != (parent_after.st_dev, parent_after.st_ino):
            raise ValueError("CLI 摘要父目录在发布期间发生变化")
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
