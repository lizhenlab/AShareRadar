"""Convert an existing Choice archive to isolated replay-verified research history."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.choice_experimental_history import build_choice_experimental_history, load_choice_experimental_history  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="offline conversion, no SDK or data requests")
    build.add_argument("--source-dir", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    verify = commands.add_parser("verify", help="read-only source and derived-row replay")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        result = build_choice_experimental_history(args.source_dir, args.output_dir).summary
    else:
        history = load_choice_experimental_history(args.manifest, args.database)
        result = {"status": "choice_experimental_history_verified", "manifest_digest": history.provenance["manifest_digest"],
                  "symbols": len(history.series), "sessions": len(history.sessions),
                  "accepted_bars": sum(len(rows) for rows in history.series.values()),
                  "formal_filter_qualified": False, "production_ranking_effect": "none"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
