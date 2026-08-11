from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.market_scan_evaluation import EvaluationConfig, evaluate_market_scan_rankings  # noqa: E402
from app.services.market_scan_probability_artifact import (  # noqa: E402
    build_probability_artifact,
    replay_probability_artifact_set,
    write_probability_artifact,
)
from app.services.market_scan_probability_research import (  # noqa: E402
    PROBABILITY_PRIMARY_TARGET,
    probability_artifact_payload,
)


def main() -> int:
    args = _parser().parse_args()
    database_digest_before = _file_sha256(args.database)
    report = evaluate_market_scan_rankings(
        args.database,
        config=EvaluationConfig(
            complete_day_coverage=args.complete_day_coverage,
            bootstrap_samples=args.bootstrap_samples,
            cost_profile=args.cost_profile,
            execution_notional=args.execution_notional,
        ),
        mode=args.mode,
        run_ids=args.run_ids,
    )
    research = report.get("probability_research")
    if not isinstance(research, dict):
        raise RuntimeError("上涨概率研究报告缺失")
    generated_at = str(research["generated_at"])
    artifacts = [
        (run_id, build_probability_artifact(payload, generated_at=generated_at))
        for run_id, payload in _run_payloads(probability_artifact_payload(research))
    ]
    targets = [
        write_probability_artifact(
            _artifact_path(args.output_dir, run_id, artifact),
            artifact,
            database_path=args.database,
        )
        for run_id, artifact in artifacts
    ]
    artifact_set_replay = replay_probability_artifact_set(targets) if targets else None
    database_digest_after = _file_sha256(args.database)
    summary = _summary(
        targets,
        artifacts,
        research,
        artifact_set_replay=artifact_set_replay,
        database_digest_before=database_digest_before,
        database_digest_after=database_digest_after,
    )
    if database_digest_before != database_digest_after:
        raise RuntimeError("只读上涨概率评估期间数据库字节发生变化，无法形成不变性证明")
    if args.report is not None:
        _write_report(args.report, summary, database_path=args.database, artifact_paths=targets)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读评估并持久化全市场 1/5/20 日上涨概率 Shadow artifact",
    )
    parser.add_argument("--database", type=Path, required=True, help="只读 AShareRadar SQLite 数据库")
    parser.add_argument("--output-dir", type=Path, required=True, help="不可变 artifact 输出目录")
    parser.add_argument("--report", type=Path, help="可选的机器可读研究摘要 JSON 输出路径")
    parser.add_argument("--mode", choices=("official", "intraday"))
    parser.add_argument("--run-id", type=int, action="append", dest="run_ids")
    parser.add_argument("--complete-day-coverage", type=float, default=0.95)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    parser.add_argument("--cost-profile", choices=("base", "conservative", "stress"), default="base")
    parser.add_argument("--execution-notional", type=float, default=100_000)
    return parser


def _run_payloads(payload: dict[str, object]) -> list[tuple[int, dict[str, object]]]:
    studies = payload.get("studies")
    records = payload.get("records")
    feature_evidence = payload.get("feature_evidence")
    contract = payload.get("record_contract_version")
    if not isinstance(studies, list) or not isinstance(records, list) or not isinstance(feature_evidence, list):
        raise RuntimeError("上涨概率 artifact payload 结构无效")
    if not isinstance(contract, str) or not contract:
        raise RuntimeError("上涨概率 artifact result contract 缺失")
    run_ids = sorted({int(item["run_id"]) for item in studies if isinstance(item, dict)})
    return [
        (
            run_id,
            {
                "record_contract_version": contract,
                "feature_evidence": [
                    item for item in feature_evidence
                    if isinstance(item, dict) and item.get("run_id") == run_id
                ],
                "studies": [item for item in studies if isinstance(item, dict) and item.get("run_id") == run_id],
                "records": [item for item in records if isinstance(item, dict) and item.get("run_id") == run_id],
            },
        )
        for run_id in run_ids
    ]


def _artifact_path(output_dir: Path, run_id: int, artifact: dict[str, object]) -> Path:
    integrity = artifact.get("integrity")
    if not isinstance(integrity, dict) or not isinstance(integrity.get("integrity_digest"), str):
        raise RuntimeError("上涨概率 artifact 完整性摘要缺失")
    digest = integrity["integrity_digest"]
    return output_dir.expanduser().resolve() / f"market-scan-probability-run-{run_id}-{digest}.json"


def _summary(
    targets: list[Path],
    artifacts: list[tuple[int, dict[str, object]]],
    research: dict[str, object],
    *,
    artifact_set_replay: dict[str, object] | None,
    database_digest_before: str,
    database_digest_after: str,
) -> dict[str, object]:
    artifact_rows = [
        {
            "run_id": run_id,
            "artifact": str(target.resolve()),
            "integrity_digest": artifact["integrity"]["integrity_digest"],  # type: ignore[index]
        }
        for target, (run_id, artifact) in zip(targets, artifacts, strict=True)
    ]
    horizons = _horizon_summaries(research)
    calibrated_horizons = [
        int(horizon)
        for horizon, evidence in horizons.items()
        if evidence["status"] == "calibrated_shadow"
    ]
    return {
        "schema_version": "market-scan-probability-evaluation-summary-v1",
        "artifact": artifact_rows[0]["artifact"] if len(artifact_rows) == 1 else None,
        "artifacts": artifact_rows,
        "artifact_count": len(artifact_rows),
        "integrity_notice": "integrity_digest_not_a_signature",
        "status": research.get("status"),
        "run_count": research.get("run_count"),
        "record_count": research.get("record_count"),
        "default_horizon": 5,
        "primary_target": PROBABILITY_PRIMARY_TARGET,
        "credible_probability_available": bool(calibrated_horizons),
        "calibrated_shadow_horizons": calibrated_horizons,
        "horizons": horizons,
        "cohorts": _cohort_summaries(research),
        "production_ranking_effect": "none",
        "production_rule": "full-market-score-v4",
        "automatic_promotion": False,
        "full_input_replay_verified": artifact_set_replay is not None,
        "artifact_set_replay": artifact_set_replay,
        "database_read_only": True,
        "database_sha256_before": database_digest_before,
        "database_sha256_after": database_digest_after,
        "database_bytes_unchanged": database_digest_before == database_digest_after,
    }


def _cohort_summaries(research: dict[str, object]) -> list[dict[str, object]]:
    raw_cohorts = research.get("cohorts")
    cohorts = raw_cohorts if isinstance(raw_cohorts, list) else []
    return [
        {
            "cohort_contract": cohort.get("cohort_contract"),
            "cohort_digest": cohort.get("cohort_digest"),
            "status": cohort.get("status"),
            "run_ids": cohort.get("run_ids") or [],
            "session_count": len(cohort.get("session_dates") or []),
            "observation_count": cohort.get("observation_count"),
            "horizons": _horizon_summaries(dict(cohort)),
        }
        for cohort in cohorts
        if isinstance(cohort, dict)
    ]


def _horizon_summaries(research: dict[str, object]) -> dict[str, dict[str, object]]:
    raw_horizons = research.get("horizons")
    horizons = raw_horizons if isinstance(raw_horizons, dict) else {}
    output: dict[str, dict[str, object]] = {}
    for horizon in ("1", "5", "20"):
        raw_targets = horizons.get(horizon)
        targets = raw_targets if isinstance(raw_targets, dict) else {}
        raw_evidence = targets.get(PROBABILITY_PRIMARY_TARGET)
        evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
        raw_promotion = evidence.get("promotion_gates")
        promotion = raw_promotion if isinstance(raw_promotion, dict) else {}
        raw_gates = promotion.get("gates")
        gates = raw_gates if isinstance(raw_gates, dict) else {}
        output[horizon] = {
            "status": evidence.get("status") or "insufficient_data",
            "probability": None,
            "counts": evidence.get("counts") or {},
            "training_cutoff": evidence.get("training_cutoff"),
            "failed_gates": sorted(name for name, passed in gates.items() if passed is not True),
            "limitations": evidence.get("limitations") or ["probability_evidence_not_generated"],
            "eligible_for_human_review": promotion.get("passed") is True,
        }
    return output


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.expanduser().resolve().open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_report(
    path: Path,
    summary: dict[str, object],
    *,
    database_path: Path,
    artifact_paths: list[Path],
) -> Path:
    target = path.expanduser().resolve()
    database = database_path.expanduser().resolve()
    if target == database or (target.exists() and os.path.samefile(target, database)):
        raise ValueError("研究摘要输出路径不能覆盖或链接到 SQLite 数据库")
    for artifact in artifact_paths:
        resolved = artifact.resolve()
        if target == resolved or (target.exists() and os.path.samefile(target, resolved)):
            raise ValueError("研究摘要输出路径不能覆盖或链接到概率 artifact")
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
    return target


if __name__ == "__main__":
    raise SystemExit(main())
