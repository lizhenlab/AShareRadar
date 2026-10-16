"""Inspect a declared runtime, plan and runner heartbeat offline; never collect or scan."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import subprocess
import sys
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, decode_json_bytes, read_regular_file, sha256_hex  # noqa: E402
from app.services.strategy_prospective_health import (  # noqa: E402
    inspect_runner_health, prospective_runner_identity, runner_health_directory,
)
from app.services.strategy_prospective_store import (  # noqa: E402
    PLAN_KEYS, decode_calendar, prospective_directory, read_prospective_record,
)
from tools.strategy_prospective_runtime import inspect_runtime  # noqa: E402


def _verify_study_binding(identity: Mapping[str, str], manifest: Mapping[str, object]) -> None:
    directory = prospective_directory(Path(identity["root"]), identity["plan_id"])
    plan = read_prospective_record(directory, "plan.json", PLAN_KEYS, "strategy-template-prospective-plan-v1")
    if plan["digest"] != identity["plan_digest"] or plan["plan_id"] != identity["plan_id"]:
        raise ValueError("prospective runtime or plan identity mismatch")
    decode_calendar(plan["calendar_base64"], plan["calendar_sha256"])
    specification = cast(dict[str, object], plan["specification"])
    files = cast(dict[str, str], manifest["code_files"])
    source = {name: digest for name, digest in files.items() if name.endswith(".py") and name.split("/")[0] in {"app", "tools"}}
    if specification["source_manifest"] != source:
        raise ValueError("prospective plan does not match the declared runtime source")
    if plan["calendar_sha256"] != files.get("app/resources/trading_calendar.json"):
        raise ValueError("prospective plan does not match the declared runtime calendar")
    binding = {"schema_version": "strategy-prospective-runner-binding-v1", "plan_id": identity["plan_id"],
               "plan_digest": identity["plan_digest"], "root": identity["root"], "database": identity["database"]}
    expected = {**binding, "digest": sha256_hex(canonical_json_bytes(binding))}
    actual = decode_json_bytes(read_regular_file(runner_health_directory(identity) / "binding.json", max_bytes=64 * 1024))
    if actual != expected:
        raise ValueError("prospective control binding mismatch")


def inspect_strategy_prospective_runtime(identity: Mapping[str, str]) -> dict[str, object]:
    """Validate the sealed copy rather than comparing a plan to this workspace's code."""
    manifest = inspect_runtime(Path(identity["runtime_directory"]), expected_digest=identity["runtime_digest"])
    _verify_study_binding(identity, manifest)
    return {"operation": "inspect_runner", "identity": dict(identity), **inspect_runner_health(identity)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--expected-runtime-digest", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--control-root", type=Path, required=True)
    parser.add_argument("--expected-plan-digest", required=True)
    args = parser.parse_args(argv)
    try:
        identity = prospective_runner_identity(args.database, args.root, args.plan_id, args.control_root,
            args.expected_plan_digest, args.directory, args.expected_runtime_digest)
        result = inspect_strategy_prospective_runtime(identity)
    except ValueError as exc:
        print(json.dumps({"operation": "inspect_runner", "status": "fault", "fault_codes": ["identity_or_health_invalid"],
                          "error_type": type(exc).__name__}))
        return 3
    except (ArtifactIOError, OSError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"operation": "inspect_runner", "status": "unknown", "fault_codes": ["inspection_unavailable"],
                          "error_type": type(exc).__name__}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return {"healthy": 0, "fault": 3, "unknown": 2}[cast(str, result["status"])]


if __name__ == "__main__":
    raise SystemExit(main())
