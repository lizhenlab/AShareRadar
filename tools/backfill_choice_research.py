"""Collect an isolated Choice research dataset with explicit, resumable scope."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish, read_regular_file, sha256_hex  # noqa: E402
from app.services.choice_quota import build_quota_report, quota_query_args, quota_query_window  # noqa: E402
from app.services.choice_research import make_plan, normalize  # noqa: E402
from app.services.choice_research_collect import ChoiceCollector, failure_summary  # noqa: E402
from app.services.choice_research_store import ChoiceBudget, ChoiceDataset, now_text  # noqa: E402
from app.services.choice_research_supplement import SUPPLEMENT_VERSION, ChoiceSupplementCollector, make_supplement_plan, supplement_coverage  # noqa: E402
from app.services.choice_research_universe import UNIVERSE_VERSION, ChoiceUniverseCollector, make_universe_plan, rebuild_universe_plan, verify_universe_bundle  # noqa: E402
from app.services.choice_sdk import ChoiceError, ChoiceSDKClient  # noqa: E402


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("operation", choices=["quota", "plan", "collect", "supplement-plan", "supplement", "universe-plan", "universe", "resume", "status", "verify"])
    result.add_argument("--output-dir", type=Path)
    result.add_argument("--source-dir", type=Path)
    result.add_argument("--reuse-dir", type=Path, action="append", default=[])
    result.add_argument("--recent-universe-sessions", type=int, default=30)
    result.add_argument("--event-limit", type=int, default=3)
    result.add_argument("--start-date")
    result.add_argument("--end-date")
    result.add_argument("--symbol-limit", type=int, default=60)
    result.add_argument("--prefer-symbol", action="append", default=[])
    result.add_argument("--event-symbol", action="append", default=[])
    result.add_argument("--max-requests", type=int, default=100)
    result.add_argument("--max-csd-cells", type=int, default=450000)
    result.add_argument("--max-css-cells", type=int, default=200000)
    result.add_argument("--max-dividend-event-calls", type=int, default=3)
    result.add_argument("--max-universe-calls", type=int, default=60)
    result.add_argument("--planning-symbols", type=int, default=60, help="quota-only arithmetic capacity assumption")
    result.add_argument("--planning-sessions", type=int, default=502, help="quota-only arithmetic capacity assumption")
    result.add_argument("--planning-snapshot-dates", type=int, default=26, help="quota-only metadata snapshot assumption")
    result.add_argument("--planning-report-dates", type=int, default=11, help="quota-only dividend snapshot assumption")
    result.add_argument("--timeout", type=float, default=45)
    result.add_argument("--request-interval", type=float, default=1.0)
    return result


def _read_existing(directory: Path, *, verify: bool) -> dict:
    with ChoiceDataset.open_readonly(directory) as dataset:
        summary = dataset.verify(normalize) if verify else dataset.summary()
        if verify and dataset.plan.get("schema_version") == UNIVERSE_VERSION:
            summary["coverage"] = verify_universe_bundle(dataset)
        if verify and dataset.plan.get("schema_version") == SUPPLEMENT_VERSION:
            with ChoiceDataset.open_readonly(Path(dataset.plan["source_directory"])) as source:
                rebuilt = make_supplement_plan(source, recent_sessions=dataset.plan["recent_universe_sessions"], event_limit=dataset.plan["event_limit"])
                if rebuilt != dataset.plan:
                    raise ChoiceError("supplement source/plan changed")
                summary["coverage"] = supplement_coverage(source, dataset)
        summary["raw_replay_verified"] = verify
        # Read-only verification proves existing records, not completeness of the plan.
        summary["status"] = "existing_records_verified" if verify else "checkpoint_status"
        return summary


def _new_supplement_plan(args: argparse.Namespace) -> dict:
    if args.source_dir is None:
        raise ChoiceError("supplement requires --source-dir")
    if args.output_dir.resolve() == args.source_dir.resolve():
        raise ChoiceError("supplement output must differ from the base dataset")
    with ChoiceDataset.open_readonly(args.source_dir) as source:
        return make_supplement_plan(source, recent_sessions=args.recent_universe_sessions, event_limit=args.event_limit)


def _selected_plan(args: argparse.Namespace) -> dict:
    if args.operation in {"universe-plan", "universe"}:
        return _new_universe_plan(args)
    if args.operation in {"supplement-plan", "supplement"}:
        return _new_supplement_plan(args)
    if args.operation == "resume":
        return _resume_plan(args.output_dir)
    if not args.start_date or not args.end_date:
        raise ChoiceError("plan/collect requires explicit --start-date and --end-date")
    if args.end_date >= datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat():
        raise ChoiceError("only completed historical dates before today may be collected")
    return make_plan(args.start_date, args.end_date, args.symbol_limit, args.prefer_symbol, args.event_symbol)


def _resume_plan(directory: Path) -> dict:
    saved = decode_json_bytes(read_regular_file(directory / "plan.json", max_bytes=1024 * 1024))
    if not isinstance(saved, dict):
        raise ChoiceError("invalid saved Choice plan")
    if saved.get("schema_version") == UNIVERSE_VERSION:
        return rebuild_universe_plan(saved)
    if saved.get("schema_version") == SUPPLEMENT_VERSION:
        with ChoiceDataset.open_readonly(Path(saved["source_directory"])) as source:
            rebuilt = make_supplement_plan(source, recent_sessions=saved["recent_universe_sessions"], event_limit=saved["event_limit"])
    else:
        rebuilt = make_plan(saved["start_date"], saved["end_date"], saved["symbol_limit"], saved["preferred_symbols"], saved["event_symbols"])
    if saved != rebuilt:
        raise ChoiceError("saved source/plan does not match the current Choice research contract")
    return rebuilt


def _new_universe_plan(args: argparse.Namespace) -> dict:
    if args.source_dir is None:
        raise ChoiceError("universe requires --source-dir")
    directories = [args.source_dir, *args.reuse_dir]
    if args.output_dir.resolve() in {directory.resolve() for directory in directories}:
        raise ChoiceError("universe output cannot overwrite a source")
    with ExitStack() as stack:
        sources = [stack.enter_context(ChoiceDataset.open_readonly(directory)) for directory in directories]
        return make_universe_plan(sources[0], sources[1:])


def _run_quota(args: argparse.Namespace) -> dict[str, Any]:
    if (not 1 <= args.planning_symbols <= 225 or not 1 <= args.planning_sessions <= 800
            or not 1 <= args.planning_snapshot_dates <= args.planning_sessions
            or not 0 <= args.planning_report_dates <= 20):
        raise ChoiceError("quota planning scope is outside the bounded research range")
    control = ROOT / "data" / "research" / "choice_ingestion_control"
    today = quota_query_window()[1]
    with ChoiceBudget(control, csd_limit=args.max_csd_cells, css_limit=args.max_css_cells,
                      ctr_limit=args.max_dividend_event_calls, sector_limit=args.max_universe_calls) as budget:
        with ChoiceSDKClient(timeout=args.timeout, interval=args.request_interval) as client:
            response = client.request("datastatistics", quota_query_args(today))
        budget.update(response)
        report = build_quota_report(budget, queried_at=now_text(), today=today,
            planning_symbols=args.planning_symbols, planning_sessions=args.planning_sessions,
            planning_snapshot_dates=args.planning_snapshot_dates,
            planning_report_dates=args.planning_report_dates)
        encoded = canonical_json_bytes(report)
        target = control / "account" / f"{sha256_hex(encoded)}.json"
        exclusive_atomic_publish(target, encoded, max_bytes=1024 * 1024)
        return report


def main() -> int:
    args = parser().parse_args()
    try:
        if args.operation == "quota":
            print(json.dumps(_run_quota(args), ensure_ascii=False))
            return 0
        if args.output_dir is None:
            raise ChoiceError("--output-dir is required except for quota")
        if args.operation in {"status", "verify"}:
            print(json.dumps(_read_existing(args.output_dir, verify=args.operation == "verify"), ensure_ascii=False))
            return 0
        plan = _selected_plan(args)
        if args.operation in {"plan", "supplement-plan", "universe-plan"}:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        control = ROOT / "data" / "research" / "choice_ingestion_control"
        with ChoiceBudget(control, csd_limit=args.max_csd_cells, css_limit=args.max_css_cells,
                          ctr_limit=args.max_dividend_event_calls, sector_limit=args.max_universe_calls) as budget:
            with ChoiceDataset(args.output_dir, plan) as dataset, ChoiceSDKClient(timeout=args.timeout, interval=args.request_interval) as client:
                collector_type = {SUPPLEMENT_VERSION: ChoiceSupplementCollector, UNIVERSE_VERSION: ChoiceUniverseCollector}.get(plan.get("schema_version", ""), ChoiceCollector)
                collector = collector_type(dataset, client, budget, max_requests=args.max_requests, progress=_progress)
                try:
                    summary = collector.run()
                except (ChoiceError, ArtifactIOError, ValueError) as exc:
                    print(json.dumps(failure_summary(dataset, collector, str(exc)), ensure_ascii=False))
                    return 2
                print(json.dumps(summary, ensure_ascii=False))
                return 0
    except (ChoiceError, ArtifactIOError, ValueError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "error": str(exc), "production_modified": False}, ensure_ascii=False), file=sys.stderr)
        return 2


def _progress(value: dict) -> None:
    count = value["requests_this_run"] + value["cached_requests"]
    if count == 1 or count % 5 == 0 or value["stage"] in {"daily", "dividend_event"}:
        print(json.dumps({"progress": value}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
