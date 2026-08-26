"""Offline personal H5 model build; no live SQLite or formal authority writes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.experimental_probability_model import MODEL_DIRECTORY, build_experimental_model  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / MODEL_DIRECTORY)
    args = parser.parse_args()
    print("Verifying historical replay and fitting isolated personal H5 model...", flush=True)
    target = build_experimental_model(args.source, args.output_dir)
    print(json.dumps({"status": "experimental_model_built", "path": str(target),
                      "formal_filter_qualified": False, "production_ranking_effect": "none"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
