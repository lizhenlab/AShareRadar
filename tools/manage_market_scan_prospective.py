"""Create future collection plans and append local-only, immutable input receipts."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import (  # noqa: E402
    ArtifactIOError, canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish, read_regular_file,
)
from app.services.market_scan_prospective import (  # noqa: E402
    bind_prospective_result, create_prospective_plan, record_prospective_input, seal_prospective_inputs, verify_prospective_plan,
)
from app.services.market_scan_prospective_contract import MAX_PLAN_BYTES, MAX_RECORD_BYTES  # noqa: E402
from app.services.market_scan_trial_registry_contract import registry_object  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="独立前瞻采集协议；仅本地证据，不认证输入来源或允许生产晋级")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "record", "verify", "seal", "bind-result"):
        command = commands.add_parser(name)
        command.add_argument("--plan-root", type=Path, required=True)
        command.add_argument("--plan-id", required=True)
        command.add_argument("--output", type=Path, help="可选不可覆盖 JSON 摘要；省略则输出到 stdout")
        if name == "create":
            command.add_argument("--spec", type=Path, required=True, help="完整未来日历、逻辑批次槽、候选、统计、源码清单及执行政策")
        elif name == "record":
            command.add_argument("--trade-date", required=True)
            inputs = command.add_mutually_exclusive_group(required=True)
            inputs.add_argument("--input", type=Path, help="含真实 run_id 和可用时间声明的原始输入 envelope")
            inputs.add_argument("--missing", action="store_true", help="截止后永久保留缺失，不允许补录")
            command.add_argument("--reason", default="", help="missing 时必须填写的原因")
        elif name == "verify":
            command.add_argument("--expected-plan-digest")
            command.add_argument("--expected-seal-digest")
        elif name == "bind-result":
            command.add_argument("--result", type=Path, required=True)
    return parser


def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "create":
        spec = registry_object(decode_json_bytes(read_regular_file(args.spec, max_bytes=MAX_PLAN_BYTES)), "specification")
        return create_prospective_plan(args.plan_root, args.plan_id, spec)
    if args.command == "record":
        return record_prospective_input(args.plan_root, args.plan_id, args.trade_date, input_path=args.input, reason=args.reason)
    if args.command == "seal":
        return seal_prospective_inputs(args.plan_root, args.plan_id)
    if args.command == "bind-result":
        return bind_prospective_result(args.plan_root, args.plan_id, args.result)
    return verify_prospective_plan(args.plan_root, args.plan_id, expected_plan_digest=args.expected_plan_digest,
                                   expected_seal_digest=args.expected_seal_digest)


def _validate_output(args: argparse.Namespace) -> None:
    if args.output is None:
        return
    output = args.output.expanduser().resolve(strict=False)
    if output.is_relative_to(args.plan_root.expanduser().resolve(strict=False)):
        raise ValueError("output must be outside the immutable plan root")
    if output.exists():
        raise ValueError("output already exists; choose a new immutable output path")


def _write_output(args: argparse.Namespace, payload: dict[str, object]) -> None:
    summary = {key: value for key, value in payload.items() if key not in {"input_base64", "result_base64"}}
    encoded = canonical_json_bytes({
        "operation": args.command, "receipt": summary, "timestamp_assurance": "local-only-unverified",
        "input_admission_verified": False, "batch_selection_verified": False, "machine_promotion_eligible": False,
    })
    if args.output is None:
        print(encoded.decode("utf-8"))
    else:
        exclusive_atomic_publish(args.output, encoded, max_bytes=MAX_RECORD_BYTES)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    completed = False
    try:
        _validate_output(args)
        payload = _execute(args)
        completed = True
        _write_output(args, payload)
        return 0
    except KeyboardInterrupt:
        print("前瞻操作已中断；请先 verify 已提交收据再恢复。", file=sys.stderr)
        return 130
    except (ArtifactIOError, OSError, ValueError, RuntimeError) as exc:
        print(f"前瞻协议失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        if completed:
            print("操作已提交；请用 verify 与新的 --output 重取收据摘要，不要重复 record。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
