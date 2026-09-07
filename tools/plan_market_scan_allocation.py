"""Create immutable proposed allocation budgets from independently pinned sources."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError, decode_json_bytes, exclusive_atomic_publish, read_regular_file  # noqa: E402
from app.services.market_scan_allocation import plan_market_scan_allocation  # noqa: E402
from app.services.market_scan_allocation_contracts import MAX_ALLOCATION_BYTES, allocation_json_bytes  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线全账户预算规划；仅生成拟买订单，不交易、不改变旧回放")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--account-digest", required=True)
    parser.add_argument("--candidates-digest", required=True)
    parser.add_argument("--market-digest", required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--policy-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _read(path: Path) -> object:
    return decode_json_bytes(read_regular_file(path, max_bytes=MAX_ALLOCATION_BYTES))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.output.expanduser().exists() or args.output.expanduser().is_symlink():
            raise ValueError("output must be a new immutable file")
        report = plan_market_scan_allocation(_read(args.input), _read(args.policy), expected_policy_digest=args.policy_digest,
                                            expected_source_digests={"account": args.account_digest, "candidates": args.candidates_digest,
                                                                     "market": args.market_digest})
        if not exclusive_atomic_publish(args.output, allocation_json_bytes(report), max_bytes=MAX_ALLOCATION_BYTES):
            raise ValueError("output already exists; no overwrite permitted")
        print(report["result_digest"])
        return 2 if report["status"] == "blocked" else 0
    except (ArtifactIOError, OSError, ValueError, RuntimeError) as error:
        print(f"组合预算规划失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
