"""Run one bounded capture cycle or watch local publication until the phase ends."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, time as clock_time
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError  # noqa: E402
from app.services.strategy_prospective_runner import (  # noqa: E402
    local_service_request, run_strategy_prospective_cycle,
)
from app.services.strategy_prospective_health import (  # noqa: E402
    ProspectiveRunnerHealth, prospective_runner_health_session, prospective_runner_identity,
)
from tools.strategy_prospective_runtime import (  # noqa: E402
    RUNTIME_DIGEST_ENV, RUNTIME_DIRECTORY_ENV, inspect_running_runtime,
)
from app.utils.clock import ASHARE_TIMEZONE, utc_now  # noqa: E402


def _watch_delay(state: dict[str, Any]) -> int:
    now = utc_now().astimezone(ASHARE_TIMEZONE)
    day = state["next_signal_date"]
    if day is None:
        return 3600
    opening = datetime.combine(datetime.fromisoformat(day).date(), clock_time(15, 15), ASHARE_TIMEZONE)
    if opening > now:
        return max(1, min(3600, int((opening - now).total_seconds())))
    return 60


def _run_watch(args: argparse.Namespace, health: ProspectiveRunnerHealth) -> int:
    fault = "cycle_failed"
    try:
        request = local_service_request(args.base_url)
        while True:
            fault = "health_write_failed"
            health.checking()
            fault = "runtime_validation_failed"
            inspect_running_runtime()
            fault = "cycle_failed"
            result = run_strategy_prospective_cycle(args.database, args.root, args.plan_id, args.control_root,
                                                     args.expected_plan_digest, request=request)
            state = cast(dict[str, Any], result["state"])
            continuing = args.watch and result["action"] != "blocked" and state["status"] != "collection_complete"
            delay = _watch_delay(state) if continuing else None
            fault = "health_write_failed"
            health.completed(result, delay)
            if not args.watch or result["meaningful_change"] or result["action"] == "scan_submitted":
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if not continuing:
                return 3 if result["fault"] or result["action"] == "blocked" else 0
            time.sleep(cast(int, delay))
    except (ArtifactIOError, OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError):
        health.failed(fault)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--control-root", type=Path, required=True)
    parser.add_argument("--expected-plan-digest", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--watch", action="store_true", help="每60秒检查，扫描结束后及时捕获；阻断或采集阶段结束时退出")
    args = parser.parse_args(argv)
    try:
        identity = prospective_runner_identity(args.database, args.root, args.plan_id, args.control_root,
            args.expected_plan_digest, Path(os.getenv(RUNTIME_DIRECTORY_ENV, "")), os.getenv(RUNTIME_DIGEST_ENV, ""))
        with prospective_runner_health_session(identity) as health:
            return _run_watch(args, health)
    except (ArtifactIOError, OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "operation": "runner", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
