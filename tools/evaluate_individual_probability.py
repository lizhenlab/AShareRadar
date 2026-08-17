#!/usr/bin/env python3
"""Build compact, immutable D+2/D+3/D+4 probability research evidence."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.individual_probability_artifact import (  # noqa: E402
    build_individual_probability_assessment,
    write_individual_probability_assessment,
)


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    artifact = build_individual_probability_assessment(
        args.history_manifest,
        official_source_paths=tuple(args.official_source),
        generated_at=args.generated_at,
    )
    target = _publish_assessment(parser, args, artifact)
    _print_summary(target, artifact)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=("从 attested 研究历史构建个股 D+2/D+3/D+4 compact Shadow 评估；" "只读输入且不写生产 SQLite"))
    parser.add_argument("--history-manifest", type=Path, required=True)
    parser.add_argument("--database", type=Path, help="运行时 SQLite；受管发布时用于原发布快照复验")
    parser.add_argument("--official-source", type=Path, action="append", default=[])
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=ROOT / "data" / "research" / "individual_probability",
    )
    parser.add_argument("--generated-at", help="可选固定 ISO 时间，用于确定性重放")
    return parser


def _publish_assessment(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    artifact: Mapping[str, object],
) -> Path:
    managed_output = ROOT / "data" / "research" / "individual_probability"
    output = args.output_directory.expanduser().absolute()
    if output.resolve(strict=False) == managed_output.resolve(strict=False) and output != managed_output:
        parser.error("受管输出目录不能通过符号链接或路径别名访问")
    if args.database is None and output == managed_output:
        parser.error("默认受管输出目录必须显式传入 --database 以复验原发布快照")
    if output == managed_output and args.database is not None and args.database.expanduser().absolute() != ROOT / "data" / "ashare_radar.sqlite3":
        parser.error("受管输出目录必须绑定同一 data 根下的运行时 SQLite")
    if args.database is None:
        target = write_individual_probability_assessment(args.output_directory, artifact)
    else:
        target = write_individual_probability_assessment(args.output_directory, artifact, database_path=args.database)
    return target


def _print_summary(target: Path, artifact: Mapping[str, object]) -> None:
    integrity = _mapping(artifact["integrity"])
    payload = _mapping(artifact["payload"])
    official_pit = _mapping(payload["official_pit"])
    horizons = _mapping(payload["horizons"])
    print(f"path={target}")
    print(f"sha256={integrity['integrity_digest']}")
    print(f"bytes={target.stat().st_size}")
    print(f"official_pit_sessions={official_pit['session_count']}")
    for key, raw_value in horizons.items():
        value = _mapping(raw_value)
        raw_metrics = value["calibration_metrics"]
        metrics = _mapping(raw_metrics) if isinstance(raw_metrics, Mapping) else {}
        counts = _mapping(value["counts"])
        print(
            " ".join(
                (
                    f"holding={key}",
                    f"display_day={value['display_day']}",
                    f"fit_status={value['fit_status']}",
                    f"selection_qualified={value['selection_qualified']}",
                    f"sessions={counts['independent_session_count']}",
                    f"oos_sessions={counts['out_of_sample_session_count']}",
                    f"brier={metrics.get('brier_score')}",
                    f"brier_skill={metrics.get('brier_skill_score')}",
                    f"auc={metrics.get('auc')}",
                )
            )
        )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("assessment projection must be an object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
