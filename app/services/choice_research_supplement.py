"""Prioritized, additive gap filling bound to a verified read-only base dataset."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any

from app.artifacts.io import canonical_json_bytes, exclusive_atomic_publish, sha256_hex
from app.services.choice_research import EVENT_FIELDS, OPTIONS, REFERENCE_FIELDS, SUSPENSION_FIELDS, normalize, request
from app.services.choice_research_collect import ChoiceCollector
from app.services.choice_research_store import ChoiceDataset, now_text
from app.services.choice_sdk import ChoiceError


SUPPLEMENT_VERSION = "choice-research-supplement-v1"


def _pairs(source: ChoiceDataset, kinds: list[str]) -> set[tuple[str, str]]:
    marks = ",".join("?" for _ in kinds)
    return set(source.db.execute(f"SELECT symbol,as_of FROM records WHERE kind IN ({marks})", kinds))


def _event_symbols(source: ChoiceDataset) -> set[str]:
    symbols: set[str] = set()
    for (value,) in source.db.execute("SELECT descriptor_json FROM requests"):
        descriptor = json.loads(value)
        if descriptor["kind"] == "dividend_event":
            symbols.update(descriptor["symbols"])
    return symbols


def source_receipts_digest(source: ChoiceDataset) -> str:
    """Bind a derived plan to the base plan and immutable request receipts."""
    rows = source.db.execute("SELECT request_key,raw_sha256,records_digest,record_count FROM requests ORDER BY request_key").fetchall()
    return sha256_hex(canonical_json_bytes({"plan": source.plan, "receipts": [list(row) for row in rows]}))


def _restricted_pairs(source: ChoiceDataset) -> set[tuple[str, str]]:
    return set(source.db.execute("""SELECT symbol,session_date FROM daily_bars
        WHERE quality IN ('suspended','unknown_or_intraday_restricted_state')"""))


def make_supplement_plan(source: ChoiceDataset, *, recent_sessions: int = 30, event_limit: int = 3) -> dict[str, Any]:
    if source.plan.get("schema_version") == SUPPLEMENT_VERSION:
        raise ChoiceError("supplements require a base history dataset, not another supplement")
    if not 0 <= recent_sessions <= 34 or not 0 <= event_limit <= 3:
        raise ChoiceError("supplement scope must be at most 34 recent universe sessions and three event symbols")
    source.verify(normalize)
    sessions = [row[0] for row in source.db.execute("SELECT DISTINCT as_of FROM records WHERE kind='calendar' ORDER BY as_of")]
    symbols = [row[0] for row in source.db.execute("SELECT DISTINCT symbol FROM daily_bars ORDER BY symbol")]
    if not sessions or not symbols or len(symbols) != source.plan.get("symbol_limit"):
        raise ChoiceError("base dataset has incomplete calendar/symbol coverage")
    if _pairs(source, ["daily"]) != {(symbol, day) for symbol in symbols for day in sessions}:
        raise ChoiceError("base dataset has incomplete daily grid")
    candidates, selected = _rank_event_candidates(source)
    plan: dict[str, Any] = {
        "schema_version": SUPPLEMENT_VERSION,
        "source_directory": str(source.directory), "source_receipts_sha256": source_receipts_digest(source),
        "start_date": sessions[0], "end_date": sessions[-1], "symbols": symbols, "sessions": sessions,
        "recent_universe_sessions": recent_sessions, "event_limit": event_limit,
        "event_symbols": selected[:event_limit], "event_candidate_factor_changes": dict(candidates),
        "priority": ["daily_price_references", "restricted_session_details", "recent_daily_universe", "dividend_event_gaps"],
        "official": False, "formal_equivalent_pit": False, "filter_qualified": False, "production_ranking_effect": "none",
        "limitations": ["retrospective_not_original_vintage", "older_daily_universe_incomplete",
                        "null_or_zero_limits_are_not_no_limit_proof", "bars_and_limits_do_not_prove_order_execution",
                        "corporate_actions_incomplete", "no_production_authorization"],
    }
    work = supplement_requests(source, plan)
    costs: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    for descriptor, units in work:
        costs[descriptor["method"]] += units
        counts[descriptor["kind"]] += 1
    plan["estimated_units"] = dict(costs)
    plan["planned_requests"] = dict(counts)
    plan["reused_reference_rows"] = len(_pairs(source, ["metadata"]))
    plan["restricted_symbol_sessions"] = len(_restricted_pairs(source))
    return plan


def _rank_event_candidates(source: ChoiceDataset) -> tuple[list[tuple[str, int]], list[str]]:
    covered = _event_symbols(source)
    changes = source.db.execute("""WITH factors AS (
        SELECT symbol,json_extract(payload_json,'$.TAFACTOR') AS f,
            LAG(json_extract(payload_json,'$.TAFACTOR')) OVER(PARTITION BY symbol ORDER BY session_date) AS previous
        FROM daily_bars)
        SELECT symbol,COUNT(*) FROM factors WHERE f>0 AND previous>0 AND abs(f/previous-1)>0.00001
        GROUP BY symbol ORDER BY COUNT(*) DESC,symbol""").fetchall()
    candidates = [(symbol, count) for symbol, count in changes if symbol not in covered]
    # Cover different markets before taking a second candidate from one market.
    selected: list[str] = []
    for symbol, _ in candidates:
        if symbol[-2:] not in {value[-2:] for value in selected}:
            selected.append(symbol)
    selected.extend(symbol for symbol, _ in candidates if symbol not in selected)
    return candidates, selected


def supplement_requests(source: ChoiceDataset, plan: dict[str, Any]) -> list[tuple[dict[str, Any], int]]:
    work: list[tuple[dict[str, Any], int]] = []
    existing = _pairs(source, ["metadata"])
    for day in plan["sessions"]:
        symbols = [symbol for symbol in plan["symbols"] if (symbol, day) not in existing]
        if symbols:
            work.append((_section("execution_reference", symbols, day, REFERENCE_FIELDS), len(symbols) * len(REFERENCE_FIELDS)))
    restricted: dict[str, list[str]] = defaultdict(list)
    for symbol, day in sorted(_restricted_pairs(source)):
        restricted[day].append(symbol)
    for day, symbols in sorted(restricted.items()):
        work.append((_section("suspension_detail", symbols, day, SUSPENSION_FIELDS), len(symbols) * len(SUSPENSION_FIELDS)))
    existing_dates = {row[0] for row in source.db.execute("SELECT DISTINCT as_of FROM records WHERE kind='universe'")}
    recent = plan["sessions"][-plan["recent_universe_sessions"]:] if plan["recent_universe_sessions"] else []
    for day in recent:
        if day not in existing_dates:
            work.append((request("universe", "sector", ["001071", day, OPTIONS], as_of=day), 1))
    for symbol in plan["event_symbols"]:
        work.append((request("dividend_event", "ctr", ["DividendImplementationInfo", ",".join(EVENT_FIELDS),
            f'secucode={symbol},StartDate={plan["start_date"]},EndDate={plan["end_date"]},DateType=1,{OPTIONS}'],
            as_of=plan["end_date"], symbols=[symbol], fields=EVENT_FIELDS), 1))
    return work


def _section(kind: str, symbols: list[str], day: str, fields: list[str]) -> dict[str, Any]:
    return request(kind, "css", [",".join(symbols), ",".join(fields), f"TradeDate={day},EndDate={day},AdjustFlag=1,{OPTIONS}"],
                   as_of=day, symbols=symbols, fields=fields)


def supplement_coverage(source: ChoiceDataset, supplement: ChoiceDataset) -> dict[str, Any]:
    references = _pairs(source, ["metadata"]) | _pairs(supplement, ["execution_reference"])
    expected = {(symbol, day) for symbol in supplement.plan["symbols"] for day in supplement.plan["sessions"]}
    if references - expected:
        raise ChoiceError("execution references outside the declared grid")
    positive, unavailable = 0, 0
    for dataset, kind in [(source, "metadata"), (supplement, "execution_reference")]:
        for (value,) in dataset.db.execute("SELECT payload_json FROM records WHERE kind=?", (kind,)):
            row = json.loads(value)
            if all(isinstance(row.get(field), (int, float)) and row[field] > 0 for field in REFERENCE_FIELDS):
                positive += 1
            else:
                unavailable += 1
    universe_dates: set[str] = set()
    for dataset in (source, supplement):
        universe_dates.update(row[0] for row in dataset.db.execute("SELECT DISTINCT as_of FROM records WHERE kind='universe'"))
    recent = supplement.plan["sessions"][-supplement.plan["recent_universe_sessions"]:] if supplement.plan["recent_universe_sessions"] else []
    restricted = _restricted_pairs(source)
    return {
        "base_database": str(source.directory / "choice_research.sqlite3"),
        "base_raw_replay_verified": True, "source_receipts_sha256": source_receipts_digest(source),
        "expected_sample_symbol_sessions": len(expected), "reference_rows_covered": len(references),
        "reference_rows_missing": len(expected - references), "positive_reference_triplets": positive,
        "reference_rows_with_null_or_zero": unavailable,
        "restricted_sessions": len(restricted), "restricted_sessions_missing_details": len(restricted - _pairs(supplement, ["suspension_detail"])),
        "full_market_universe_dates": len(universe_dates), "recent_universe_sessions_requested": len(recent),
        "recent_universe_dates_missing": sorted(set(recent) - universe_dates),
        "older_universe_dates_missing": len(set(supplement.plan["sessions"]) - universe_dates),
        "dividend_history_symbols_queried": sorted(_event_symbols(source) | _event_symbols(supplement)),
        "complete_corporate_action_coverage": False,
    }


class ChoiceSupplementCollector(ChoiceCollector):
    def run(self) -> dict[str, Any]:
        plan = self.dataset.plan
        with ChoiceDataset.open_readonly(Path(plan["source_directory"])) as source:
            rebuilt = make_supplement_plan(source, recent_sessions=plan["recent_universe_sessions"], event_limit=plan["event_limit"])
            if rebuilt != plan:
                raise ChoiceError("supplement source/plan changed; refusing mismatched resume")
            self.refresh_quota()
            for descriptor, units in supplement_requests(source, plan):
                self.fetch(descriptor, units)
            self.refresh_quota()
            summary = self.dataset.verify(normalize)
            source.verify(normalize)
            if source_receipts_digest(source) != plan["source_receipts_sha256"]:
                raise ChoiceError("supplement source changed during collection")
            coverage = supplement_coverage(source, self.dataset)
            if coverage["reference_rows_missing"] or coverage["restricted_sessions_missing_details"] or coverage["recent_universe_dates_missing"]:
                raise ChoiceError("supplement completed requests but declared coverage is incomplete")
            summary.update({"status": "complete_for_declared_research_scope", "generated_at": now_text(),
                            "requests_this_run": self.calls, "cached_requests": self.cached, "raw_replay_verified": True,
                            "coverage": coverage, "limitations": plan["limitations"], "quota_snapshot": self.budget.quotas})
        encoded = canonical_json_bytes(summary)
        output = self.dataset.directory / f"summary-{sha256_hex(encoded)}.json"
        exclusive_atomic_publish(output, encoded, max_bytes=1024 * 1024)
        summary["summary_path"] = str(output)
        return summary
