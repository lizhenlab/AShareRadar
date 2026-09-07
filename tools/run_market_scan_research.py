"""Register, execute and independently verify immutable research bundles."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import (  # noqa: E402
    ArtifactIOError, canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish, read_regular_file, sha256_hex,
)
from app.services.market_scan_official_execution import OfficialExecutionSessionRow, VerifiedOfficialExecutionSession  # noqa: E402
from app.services.market_scan_official_execution_store import MarketScanOfficialExecutionStore  # noqa: E402
from app.services.market_scan_research_experiment import (  # noqa: E402
    FrozenResearchDataset, build_research_trial_contract, prepare_research_dataset, research_execution_manifest,
)
from app.services.market_scan_research_runner import run_registered_research  # noqa: E402
from app.services.market_scan_trial_registry import create_trial_registry, load_trial_registry, verify_trial_registry  # noqa: E402
from app.services.market_scan_trial_registry_contract import registry_hash, registry_object, trial_registry_digest  # noqa: E402
from app.services.trading_calendar import trading_dates_between  # noqa: E402


RESEARCH_INPUT_BUNDLE_VERSION = "market-scan-research-input-bundle-v1"
RESEARCH_BUNDLE_MAX_BYTES = 256 * 1024 * 1024
_COMMON_BUNDLE_KEYS = frozenset({"schema_version", "calendar", "exploration_cutoff", "execution_mode"})


@dataclass(frozen=True)
class LoadedResearchBundle:
    dataset: FrozenResearchDataset
    calendar: dict[str, object]
    exploration_cutoff: str
    official_sessions: tuple[VerifiedOfficialExecutionSession, ...]
    synthetic_rows: tuple[OfficialExecutionSessionRow, ...]


def load_research_bundle(
    path: Path, *, official_registry_digest: str | None = None,
    snapshot_observer: Callable[[Mapping[str, object]], None] | None = None,
) -> LoadedResearchBundle:
    """Require explicit synthetic mode or an independently pinned official store."""
    payload = _read_object(path)
    mode = payload.get("execution_mode")
    execution_key = "synthetic_rows" if mode == "synthetic" else "official_store"
    snapshot_key = "snapshots" if "snapshots" in payload else "snapshot_files"
    if not isinstance(mode, str) or mode not in {"synthetic", "official"} or set(payload) != _COMMON_BUNDLE_KEYS | {execution_key, snapshot_key}:
        raise ValueError("bundle must declare exactly one explicit synthetic or official execution mode")
    if payload["schema_version"] != RESEARCH_INPUT_BUNDLE_VERSION:
        raise ValueError("unsupported research input bundle version")
    calendar = registry_object(payload["calendar"], "calendar")
    _validate_calendar(calendar)
    cutoff = payload["exploration_cutoff"]
    if not isinstance(cutoff, str):
        raise ValueError("exploration_cutoff must be an ISO date")
    official, synthetic = _execution_inputs(payload, path.parent, official_registry_digest)
    dataset = prepare_research_dataset(_observed_snapshots(payload, path.parent, snapshot_observer))
    return LoadedResearchBundle(dataset, calendar, cutoff, official, synthetic)


def _observed_snapshots(
    payload: Mapping[str, object], base: Path, observer: Callable[[Mapping[str, object]], None] | None,
) -> Iterator[dict[str, object]]:
    for snapshot in _snapshots(payload, base):
        if observer is not None:
            observer(snapshot)
        yield snapshot


def _snapshots(payload: Mapping[str, object], base: Path) -> Iterator[dict[str, object]]:
    if "snapshots" in payload:
        yield from _object_list(payload["snapshots"], "snapshots")
        return
    for reference in _object_list(payload["snapshot_files"], "snapshot_files"):
        if set(reference) != {"path", "sha256"}:
            raise ValueError("snapshot_files entries require exactly path and sha256")
        expected = registry_hash(reference["sha256"], "snapshot file SHA-256")
        source = _input_path(reference["path"], base)
        yield _read_snapshot(source, expected)


def _read_snapshot(source: Path, expected_digest: str) -> dict[str, object]:
    encoded = read_regular_file(source, max_bytes=RESEARCH_BUNDLE_MAX_BYTES)
    if sha256_hex(encoded) != expected_digest:
        raise ValueError("snapshot file SHA-256 mismatch")
    return registry_object(decode_json_bytes(encoded), "snapshot file")


def _execution_inputs(
    payload: Mapping[str, object], base: Path, registry_digest: str | None,
) -> tuple[tuple[VerifiedOfficialExecutionSession, ...], tuple[OfficialExecutionSessionRow, ...]]:
    if payload["execution_mode"] == "synthetic":
        if registry_digest is not None:
            raise ValueError("synthetic input cannot claim an official registry pin")
        rows = _object_list(payload["synthetic_rows"], "synthetic_rows")
        return (), tuple(OfficialExecutionSessionRow.model_validate(row) for row in rows)
    config = registry_object(payload["official_store"], "official_store")
    if set(config) != {"registry_path", "raw_file_root", "session_directory"}:
        raise ValueError("official_store requires explicit registry_path, raw_file_root and session_directory")
    pinned = registry_hash(registry_digest, "--official-registry-digest")
    store = MarketScanOfficialExecutionStore(
        registry_path=_input_path(config["registry_path"], base), registry_digest=pinned,
        raw_file_root=_input_path(config["raw_file_root"], base),
        session_directory=_input_path(config["session_directory"], base),
    )
    sessions = store.sessions()
    if not sessions:
        raise ValueError("official store contains no verified execution sessions")
    return sessions, ()


def _input_path(value: object, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("input paths must be explicit nonempty strings")
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _object_list(value: object, name: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return [registry_object(item, name) for item in value]


def _validate_calendar(calendar: Mapping[str, object]) -> None:
    dates = calendar.get("trading_dates")
    if not isinstance(dates, list) or len(dates) < 2 or any(not isinstance(day, str) for day in dates):
        raise ValueError("calendar requires an explicit complete trading_dates array")
    expected = [day.isoformat() for day in trading_dates_between(date.fromisoformat(dates[0]), date.fromisoformat(dates[-1]))]
    if dates != expected:
        raise ValueError("calendar must exactly match the complete trusted exchange calendar")


def _read_object(path: Path) -> dict[str, object]:
    return registry_object(decode_json_bytes(read_regular_file(path, max_bytes=RESEARCH_BUNDLE_MAX_BYTES)), path.name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="冻结研究试验、运行共享资金回放、独立重放核验；不写入真实运行数据库")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("register", "run", "verify", "replay", "export-registration"):
        sub = commands.add_parser(command)
        sub.add_argument("--registry-root", type=Path, required=True)
        sub.add_argument("--registration-id", required=True)
        sub.add_argument("--output", type=Path, required=command != "verify", help="不可覆盖的JSON输出文件")
        if command != "export-registration":
            sub.add_argument("--bundle", type=Path, required=True)
            sub.add_argument("--official-registry-digest", help="官方输入的独立SHA256登记锚点；合成模式不得提供")
        if command == "register":
            sub.add_argument("--registration-kind", choices=("retrospective", "prospective"), default="retrospective")
            sub.add_argument("--initial-cash", type=float, default=1_000_000)
            sub.add_argument("--cost-profile", choices=("base", "conservative", "stress"), default="base")
            sub.add_argument("--max-participation-rate", type=float, default=.01)
        elif command == "run":
            sub.add_argument("--resume", action="store_true", help="继续未封存试验登记，保留既有失败和取消记录")
        elif command == "verify":
            sub.add_argument("--report", type=Path, required=True, help="独立重放必须逐内容吻合的已保存研究报告")
    return parser


def _register(args: argparse.Namespace, bundle: LoadedResearchBundle) -> dict[str, object]:
    contract = build_research_trial_contract(
        bundle.dataset, bundle.calendar,
        execution_manifest_digest=research_execution_manifest(bundle.official_sessions, bundle.synthetic_rows),
        exploration_cutoff=bundle.exploration_cutoff, registration_kind=args.registration_kind,
        initial_cash=args.initial_cash, cost_profile=args.cost_profile, max_participation_rate=args.max_participation_rate,
    )
    registration = create_trial_registry(args.registry_root, args.registration_id, contract)
    return _registration_receipt(args, registration)


def _registration_receipt(args: argparse.Namespace, registration: Mapping[str, object]) -> dict[str, object]:
    return {"schema_version": "market-scan-research-registration-receipt-v1", "registration": registration,
            "verification": verify_trial_registry(args.registry_root, args.registration_id)}


def _execute(args: argparse.Namespace, bundle: LoadedResearchBundle) -> dict[str, object]:
    _validate_registered_split(args, bundle)
    report = run_registered_research(
        args.registry_root, args.registration_id, bundle.dataset,
        official_sessions=bundle.official_sessions, synthetic_rows=bundle.synthetic_rows,
        replay_only=args.command in {"verify", "replay"}, resume=getattr(args, "resume", False),
    )
    if args.command != "verify":
        return report
    saved = _read_object(args.report)
    saved_digest = registry_hash(saved.get("digest"), "saved report digest")
    if trial_registry_digest({key: value for key, value in saved.items() if key != "digest"}) != saved_digest:
        raise ValueError("saved report digest mismatch")
    if canonical_json_bytes(saved) != canonical_json_bytes(report):
        raise ValueError("independent replay does not match the saved report")
    return {"schema_version": "market-scan-research-replay-verification-v1", "report_digest": saved_digest,
            "independent_replay": "matched", "registry": report["registry"], "promotion_eligible": False}


def _validate_registered_split(args: argparse.Namespace, bundle: LoadedResearchBundle) -> None:
    state = load_trial_registry(args.registry_root, args.registration_id)
    contract = registry_object(state.registration["contract"], "contract")
    expected = {name: contract[name] for name in ("calendar", "exploration_cutoff")}
    supplied = {"calendar": bundle.calendar, "exploration_cutoff": bundle.exploration_cutoff}
    if canonical_json_bytes(supplied) != canonical_json_bytes(expected):
        raise ValueError("bundle calendar or exploration cutoff differs from frozen registration")


def _publish_output(args: argparse.Namespace, payload: Mapping[str, object]) -> None:
    encoded = canonical_json_bytes(dict(payload))
    if args.output is None:
        print(encoded.decode())
        return
    exclusive_atomic_publish(args.output, encoded, max_bytes=RESEARCH_BUNDLE_MAX_BYTES)
    print(f"已保存 {args.command} 结果：{args.output}", file=sys.stderr)


def _recoverable_publish(args: argparse.Namespace, payload: Mapping[str, object]) -> None:
    try:
        _publish_output(args, payload)
    except (ArtifactIOError, OSError, ValueError, RuntimeError):
        recovery = {"register": "export-registration", "run": "replay"}.get(args.command, args.command)
        print(f"研究操作已完成，输出未确认保存；使用 {recovery} 和新的 --output 路径重取结果。", file=sys.stderr)
        raise


def _validate_output(args: argparse.Namespace) -> None:
    if args.output is None:
        return
    registry_root = args.registry_root.expanduser().resolve(strict=False)
    output = args.output.expanduser().resolve(strict=False)
    if output.is_relative_to(registry_root):
        raise ValueError("output must be outside the immutable registry root")
    if output.exists():
        raise ValueError("output already exists; use a new immutable output path")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "register" and args.registration_kind == "prospective":
            raise ValueError("冻结结果 bundle 仅支持 retrospective 登记；prospective 需要独立的未来输入登记协议")
        _validate_output(args)
        if args.command == "export-registration":
            state = load_trial_registry(args.registry_root, args.registration_id)
            payload = _registration_receipt(args, state.registration)
        else:
            bundle = load_research_bundle(args.bundle, official_registry_digest=args.official_registry_digest)
            payload = _register(args, bundle) if args.command == "register" else _execute(args, bundle)
        _recoverable_publish(args, payload)
        results = payload.get("results", {})
        failed = any(registry_object(result, "trial").get("status") != "succeeded"
                     for result in registry_object(results, "results").values())
        return 1 if failed else 0
    except KeyboardInterrupt:
        print("研究执行已取消；已开始的试验保留取消回执。", file=sys.stderr)
        return 130
    except (ArtifactIOError, OSError, ValueError, RuntimeError) as exc:
        print(f"研究操作失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
