from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from app.services.market_scan_probability_historical_context import (  # noqa: E402
    HistoricalProbabilityContextError,
    publish_historical_probability_context,
)


def main() -> int:
    args = _parser().parse_args()
    try:
        target = publish_historical_probability_context(args.artifact, args.output_dir)
    except (HistoricalProbabilityContextError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": "market-scan-probability-historical-context-cli-v1",
                    "status": "failed",
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "schema_version": "market-scan-probability-historical-context-cli-v1",
                "status": "ready",
                "artifact": str(target),
                "official": False,
                "filter_qualified": False,
                "production_ranking_effect": "none",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="深度校验完整历史重放并发布轻量、只读、非授权研究上下文",
    )
    parser.add_argument("--artifact", type=Path, required=True, help="完整 historical replay artifact")
    parser.add_argument("--output-dir", type=Path, required=True, help="与完整 artifact 相同的目录")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
