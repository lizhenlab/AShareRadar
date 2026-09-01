"""Compare archived Choice/Tencent daily history offline; never fit or replace a model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, exclusive_atomic_publish, path_has_only_trusted_aliases, sha256_hex  # noqa: E402
from app.services.choice_history_comparison import compare_choice_tencent_history  # noqa: E402
from app.services.choice_sdk import ChoiceError  # noqa: E402
from app.services.market_scan_probability_history import ProbabilityHistoryError  # noqa: E402


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--choice-dir", type=Path, required=True)
    result.add_argument("--tencent-database", type=Path, required=True)
    result.add_argument("--tencent-manifest", type=Path, required=True)
    result.add_argument("--output-dir", type=Path, help="Optionally publish a new content-addressed report outside source archives")
    return result


def _publish_report(report: dict, args: argparse.Namespace) -> Path:
    output = args.output_dir.expanduser().absolute()
    protected = (args.choice_dir.resolve(), args.tencent_database.resolve().parent, args.tencent_manifest.resolve().parent)
    if ".workbuddy-ai" in output.parts or not path_has_only_trusted_aliases(output):
        raise ChoiceError("comparison report output is protected or traverses an alias")
    if any(output.resolve().is_relative_to(source) for source in protected):
        raise ChoiceError("comparison report must be outside all source archives")
    encoded = canonical_json_bytes(report)
    target = output / f"choice-tencent-comparison-{sha256_hex(encoded)}.json"
    exclusive_atomic_publish(target, encoded, max_bytes=8 * 1024 * 1024)
    return target


def main() -> int:
    args = parser().parse_args()
    try:
        report = compare_choice_tencent_history(args.choice_dir, args.tencent_database, args.tencent_manifest)
        if args.output_dir is not None:
            target = _publish_report(report, args)
            print(json.dumps({"status": "comparison_published", "report": str(target), "source_equivalence": "not_established",
                              "production_ranking_effect": "none", "runtime_model_replacement": False}, ensure_ascii=False))
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ChoiceError, ProbabilityHistoryError, ArtifactIOError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "comparison_failed", "error_type": type(exc).__name__, "source_modified": False,
                          "sdk_requests": 0, "runtime_model_replacement": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
