from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence, TextIO, cast


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from app.services.market_scan_official_execution import (  # noqa: E402
    OfficialExecutionIntakeError,
    OfficialExecutionSessionArtifact,
    OfficialExecutionSourceRegistry,
    load_verified_official_execution_session,
)
from app.services.market_scan_official_execution_store import (  # noqa: E402
    MarketScanOfficialExecutionStore,
    official_execution_session_filename,
)


CLI_SCHEMA_VERSION = "market-scan-official-execution-intake-cli-v1"
CONTRACT_SCHEMA_VERSION = "market-scan-official-execution-contract-schema-v1"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.operation == "contract":
            payload = _contract_payload()
        else:
            payload = _execute(_store(args), args)
    except (OfficialExecutionIntakeError, OSError, ValueError) as exc:
        _emit(
            {
                "schema_version": CLI_SCHEMA_VERSION,
                "operation": args.operation,
                "status": "failed",
                "error": str(exc),
            },
            stream=sys.stderr,
        )
        return 2
    _emit(payload, stream=sys.stdout)
    status = payload.get("store_status")
    return 2 if isinstance(status, dict) and status.get("status") == "verification_failed" else 0


def _store(args: argparse.Namespace) -> MarketScanOfficialExecutionStore:
    missing = [
        flag
        for flag, value in (
            ("--registry-path", args.registry_path),
            ("--registry-digest", args.registry_digest),
            ("--raw-root", args.raw_root),
            ("--session-directory", args.session_directory),
        )
        if value is None or value == ""
    ]
    if missing:
        raise OfficialExecutionIntakeError(
            "official execution operation requires store arguments: " + ", ".join(missing)
        )
    return MarketScanOfficialExecutionStore(
        registry_path=cast(Path, args.registry_path),
        registry_digest=cast(str, args.registry_digest),
        raw_file_root=cast(Path, args.raw_root),
        session_directory=cast(Path, args.session_directory),
    )


def _contract_payload() -> dict[str, object]:
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "operation": "contract",
        "status": "ready",
        "authorizing": False,
        "required_markets": ["SH", "SZ", "BJ"],
        "required_session_path": ["D", "D+1", "D+2", "D+3", "D+4", "D+5", "D+6"],
        "registry_json_schema": OfficialExecutionSourceRegistry.model_json_schema(
            mode="validation"
        ),
        "session_json_schema": OfficialExecutionSessionArtifact.model_json_schema(
            mode="validation"
        ),
        "integrity_notice": (
            "schema output is non-authorizing; verify still requires an independently pinned "
            "registry digest and exact licensed raw-file bytes"
        ),
    }


def _execute(
    store: MarketScanOfficialExecutionStore,
    args: argparse.Namespace,
) -> dict[str, object]:
    target: str | None = None
    artifact_digest: str | None = None
    session_date: str | None = None
    if args.operation == "verify":
        verified = load_verified_official_execution_session(
            args.candidate,
            registry=store.registry(),
            raw_file_root=store.raw_file_root,
        )
        target = str(store.session_directory / official_execution_session_filename(verified))
        artifact_digest, session_date = verified.artifact_digest, verified.session_date
    elif args.operation == "ingest":
        published = store.ingest(args.candidate)
        target = str(published)
        session_date, artifact_digest = _filename_identity(published)
    status = store.status().payload()
    return {
        "schema_version": CLI_SCHEMA_VERSION,
        "operation": args.operation,
        "status": "ready" if status["status"] in {"ready", "waiting_sessions"} else status["status"],
        "session_date": session_date,
        "artifact_digest": artifact_digest,
        "managed_target": target,
        "store_status": status,
    }


def _filename_identity(path: Path) -> tuple[str, str]:
    parts = path.stem.split("-")
    return "-".join(parts[-4:-1]), parts[-1]


def _emit(payload: dict[str, object], *, stream: TextIO) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stream)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="严格校验并原子安装 licensed official execution session",
    )
    parser.add_argument("--registry-path", type=Path)
    parser.add_argument("--registry-digest")
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--session-directory", type=Path)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser(
        "contract",
        help="只读输出 registry/session JSON Schema；不生成授权或摘要",
    )
    subparsers.add_parser("status", help="重放 registry 与已安装 session，只读输出状态")
    for operation in ("verify", "ingest"):
        command = subparsers.add_parser(operation)
        command.add_argument("--candidate", type=Path, required=True)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
