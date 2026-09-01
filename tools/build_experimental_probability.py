"""Offline personal H5 or D+1/D+2/D+5 model build; no formal authority writes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.experimental_probability_model import DIRECTION_OFFSETS, MODEL_DIRECTORY, build_experimental_model  # noqa: E402
from app.services.experimental_direction_model import build_choice_direction_model, build_direction_model  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prediction-kind", choices=["net_h5", *DIRECTION_OFFSETS], default="net_h5")
    parser.add_argument("--source", type=Path, help="H5: verified historical replay JSON")
    history_source = parser.add_mutually_exclusive_group()
    history_source.add_argument("--history-manifest", type=Path, help="D+1/D+2/D+5: attested static-history manifest")
    history_source.add_argument("--choice-history-manifest", type=Path, help="Choice reconstructed history; isolated candidate output only")
    parser.add_argument("--database", type=Path, help="D+1/D+2/D+5: matching read-only static qfq SQLite")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / MODEL_DIRECTORY)
    args = parser.parse_args()
    if args.prediction_kind == "net_h5":
        if args.source is None or args.history_manifest is not None or args.choice_history_manifest is not None or args.database is not None:
            parser.error("net_h5 requires --source only; history manifests are for close_d1/close_d2/close_d5")
        print("Verifying historical replay and fitting isolated personal H5 model...", flush=True)
        target = build_experimental_model(args.source, args.output_dir)
    else:
        if args.source is not None or (args.history_manifest is None and args.choice_history_manifest is None) or args.database is None:
            parser.error("close_d1/close_d2/close_d5 require --history-manifest (or --choice-history-manifest) and --database, not H5 --source")
        print(f"Verifying static history and fitting isolated {args.prediction_kind} model...", flush=True)
        if args.choice_history_manifest is not None:
            if args.output_dir == ROOT / "data" / MODEL_DIRECTORY:
                parser.error("Choice candidates require a separate --output-dir; runtime model replacement is not permitted")
            target = build_choice_direction_model(args.choice_history_manifest, args.database, args.output_dir,
                                                 offset=DIRECTION_OFFSETS[args.prediction_kind])
        else:
            target = build_direction_model(args.history_manifest, args.database, args.output_dir,
                                           offset=DIRECTION_OFFSETS[args.prediction_kind])
    print(json.dumps({"status": "experimental_model_built", "path": str(target),
                      "prediction_kind": args.prediction_kind,
                      "formal_filter_qualified": False, "production_ranking_effect": "none"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
