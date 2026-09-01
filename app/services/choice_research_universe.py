"""Complete missing daily universe observations without redownloading known dates."""

from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from itertools import groupby
from pathlib import Path
from typing import Any

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish, read_regular_file, sha256_hex
from app.services.choice_research import OPTIONS, normalize, request
from app.services.choice_research_collect import ChoiceCollector
from app.services.choice_research_store import ChoiceDataset, now_text
from app.services.choice_quota import public_quotas
from app.services.choice_research_supplement import source_receipts_digest
from app.services.choice_sdk import ChoiceError


UNIVERSE_VERSION = "choice-research-daily-universe-v1"


def _fingerprint(source: ChoiceDataset) -> dict[str, str]:
    saved = decode_json_bytes(read_regular_file(source.directory / "plan.json", max_bytes=1024 * 1024))
    if saved != source.plan:
        raise ChoiceError("source plan changed while open")
    return {"directory": str(source.directory), "receipts_sha256": source_receipts_digest(source)}


def _memberships(sources: list[ChoiceDataset]) -> dict[str, dict[str, Any]]:
    """Duplicates must agree on membership; download-time names are not features."""
    output: dict[str, dict[str, Any]] = {}
    for source in sources:
        cursor = source.db.execute("SELECT as_of,symbol FROM records WHERE kind='universe' ORDER BY as_of,symbol")
        for day, rows in groupby(cursor, key=lambda row: row[0]):
            symbols = [row[1] for row in rows]
            if len(symbols) != len(set(symbols)):
                raise ChoiceError("duplicate universe membership rows for a date")
            value = {"member_count": len(symbols), "market_counts": dict(Counter(symbol[-2:] for symbol in symbols)),
                     "membership_sha256": sha256_hex(canonical_json_bytes(symbols))}
            if day in output and output[day] != value:
                raise ChoiceError("reused universe datasets disagree for the same date")
            output[day] = value
    return output


def make_universe_plan(base: ChoiceDataset, reused: list[ChoiceDataset]) -> dict[str, Any]:
    sources = [base, *reused]
    if base.plan.get("schema_version") != "choice-research-dataset-v1":
        raise ChoiceError("daily universe expansion needs a base history calendar")
    if len(sources) > 8 or len({source.directory for source in sources}) != len(sources):
        raise ChoiceError("universe sources must be distinct, with at most eight datasets")
    for source in sources:
        source.verify(normalize)
    sessions = [row[0] for row in base.db.execute("SELECT DISTINCT as_of FROM records WHERE kind='calendar' ORDER BY as_of")]
    if not sessions or len(sessions) > 800:
        raise ChoiceError("empty or excessive universe calendar")
    existing = _memberships(sources)
    missing = sorted(set(sessions) - set(existing), reverse=True)
    if len(missing) > 600:
        raise ChoiceError("universe expansion exceeds the bounded request budget")
    return {
        "schema_version": UNIVERSE_VERSION, "start_date": sessions[0], "end_date": sessions[-1],
        "sources": [_fingerprint(source) for source in sources], "sessions": sessions,
        "missing_dates": missing, "reused_date_count": len(set(sessions) & set(existing)),
        "estimated_units": {"sector": len(missing)}, "request_order": "most_recent_missing_first",
        "sector_code": "001071", "minimum_adjacent_market_ratio": 0.95,
        "official": False, "formal_equivalent_pit": False, "filter_qualified": False,
        "production_ranking_effect": "none",
        "limitations": ["retrospective_provider_membership_not_original_vintage", "historical_names_not_proven",
                        "BJ_historical_membership_requires_independent_verification",
                        "no_independent_exchange_universe_reconciliation", "sector_quota_not_separately_reported",
                        "bars_corporate_actions_rules_and_execution_evidence_not_completed_by_universe"]}


def rebuild_universe_plan(plan: dict[str, Any]) -> dict[str, Any]:
    with ExitStack() as stack:
        sources = [stack.enter_context(ChoiceDataset.open_readonly(Path(item["directory"]))) for item in plan["sources"]]
        rebuilt = make_universe_plan(sources[0], sources[1:])
        if rebuilt != plan:
            raise ChoiceError("daily universe source/plan changed")
        return rebuilt


def _check_continuity(dates: dict[str, dict[str, Any]], plan: dict[str, Any]) -> None:
    indexes = {day: index for index, day in enumerate(plan["sessions"])}
    selected = sorted(day for day in dates if day in plan["sessions"])
    for day in selected:
        if set(dates[day]["market_counts"]) != {"SH", "SZ", "BJ"}:
            raise ChoiceError("daily universe missing a market")
    for previous, current in zip(selected, selected[1:], strict=False):
        # Compare only adjacent trading dates, never monthly gaps: a legitimate
        # long-term growth in BSE listings must not be confused with truncation.
        if indexes[current] - indexes[previous] != 1:
            continue
        for market in ("SH", "SZ", "BJ"):
            before, after = dates[previous]["market_counts"][market], dates[current]["market_counts"][market]
            if min(before, after) / max(before, after) < plan["minimum_adjacent_market_ratio"]:
                raise ChoiceError(f"daily universe continuity requires review: {previous}/{current} {market}; raw data retained")


def universe_coverage(plan: dict[str, Any], datasets: list[ChoiceDataset]) -> dict[str, Any]:
    dates = _memberships(datasets)
    _check_continuity(dates, plan)
    selected = sorted(set(plan["sessions"]) & set(dates))
    return {
        "calendar_sessions": len(plan["sessions"]), "universe_dates_covered": len(selected),
        "universe_dates_missing": sorted(set(plan["sessions"]) - set(dates)),
        "membership_rows": sum(dates[day]["member_count"] for day in selected),
        "market_count_ranges": {market: {"min": min(dates[day]["market_counts"][market] for day in selected),
                                          "max": max(dates[day]["market_counts"][market] for day in selected)} for market in ("SH", "SZ", "BJ")} if selected else {},
        "daily_membership_digest": sha256_hex(canonical_json_bytes([{ "date": day, **dates[day]} for day in selected])),
        "independent_exchange_membership_verified": False,
    }


def verify_universe_bundle(dataset: ChoiceDataset) -> dict[str, Any]:
    with ExitStack() as stack:
        sources = [stack.enter_context(ChoiceDataset.open_readonly(Path(item["directory"]))) for item in dataset.plan["sources"]]
        rebuilt = make_universe_plan(sources[0], sources[1:])
        if rebuilt != dataset.plan:
            raise ChoiceError("daily universe source/plan changed")
        return universe_coverage(dataset.plan, [*sources, dataset])


class ChoiceUniverseCollector(ChoiceCollector):
    def run(self) -> dict[str, Any]:
        plan = rebuild_universe_plan(self.dataset.plan)
        with ExitStack() as stack:
            sources = [stack.enter_context(ChoiceDataset.open_readonly(Path(item["directory"]))) for item in plan["sources"]]
            dates = _memberships(sources)
        self.refresh_quota()
        for day in plan["missing_dates"]:
            records = self.fetch(request("universe", "sector", [plan["sector_code"], day, OPTIONS], as_of=day), 1)
            dates[day] = {"market_counts": dict(Counter(record[1][-2:] for record in records))}
            _check_continuity(dates, plan)
        self.refresh_quota()
        summary = self.dataset.verify(normalize)
        coverage = verify_universe_bundle(self.dataset)
        if coverage["universe_dates_missing"]:
            raise ChoiceError("daily universe plan is incomplete")
        summary.update({"status": "complete_for_declared_research_scope", "generated_at": now_text(),
                        "requests_this_run": self.calls, "cached_requests": self.cached, "raw_replay_verified": True,
                        "coverage": coverage, "limitations": plan["limitations"],
                        "quota_snapshot": public_quotas(self.budget.quotas)})
        encoded = canonical_json_bytes(summary)
        output = self.dataset.directory / f"summary-{sha256_hex(encoded)}.json"
        exclusive_atomic_publish(output, encoded, max_bytes=1024 * 1024)
        summary["summary_path"] = str(output)
        return summary
