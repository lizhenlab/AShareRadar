"""Pure descriptive availability summaries; never fill outcomes or infer returns."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from statistics import fmean

from app.services.market_scan_research_availability_contract import (
    AVAILABILITY_STATUSES, AvailabilityBatch, AvailabilityMember,
)


def availability_status_counts(members: Sequence[AvailabilityMember]) -> dict[str, int]:
    counts = Counter(member.status for member in members)
    return {status: counts[status] for status in AVAILABILITY_STATUSES}


def availability_groups(members: Sequence[AvailabilityMember]) -> dict[str, object]:
    output: dict[str, object] = {}
    for name in ("market", "industry", "metadata_source", "quote_source", "kline_source", "error_category"):
        grouped: dict[str, list[AvailabilityMember]] = defaultdict(list)
        for member in members:
            grouped[getattr(member, name)].append(member)
        output[name] = [{"value": value, "member_count": len(rows), "status_counts": availability_status_counts(rows)}
                        for value, rows in sorted(grouped.items())]
    return output


def availability_predecision_differences(members: Sequence[AvailabilityMember]) -> dict[str, object]:
    groups = {"observed": [item for item in members if item.status == "success"],
              "unobserved": [item for item in members if item.status in {"missing", "pending"}]}
    counts = {name: len(rows) for name, rows in groups.items()}
    values = _predecision_amounts(groups)
    complete = all(counts[name] > 0 and len(values[name]) == counts[name] for name in groups)
    means = {name: fmean(group) if complete else None for name, group in values.items()}
    difference = fmean(values["observed"]) - fmean(values["unobserved"]) if complete else None
    return {
        "status": "descriptive_only" if complete else "insufficient_evidence",
        "feature": "decision_time_quote_amount", "population_counts": counts,
        "evidence_counts": {name: len(group) for name, group in values.items()},
        "means": means, "observed_minus_unobserved": difference,
        "reason": None if complete else "complete_context_verified_predecision_evidence_required_in_both_groups",
        "inference": "none; missingness mechanism and causal effects are not identified",
    }


def _predecision_amounts(groups: dict[str, list[AvailabilityMember]]) -> dict[str, list[float]]:
    return {name: [item.predecision_amount for item in rows if item.predecision_amount is not None] for name, rows in groups.items()}


def availability_missing_streaks(dates: Sequence[str], batches: Sequence[AvailabilityBatch]) -> list[dict[str, object]]:
    by_date = {batch.data_date: batch for batch in batches}
    active: dict[str, tuple[str, str, int]] = {}
    output: list[dict[str, object]] = []
    for day in dates:
        batch = by_date.get(day)
        missing = {item.symbol for item in batch.members if item.status == "missing"} if batch else set()
        for symbol in sorted(set(active) - missing):
            output.append(_streak_record(symbol, active.pop(symbol)))
        for symbol in sorted(missing):
            first, _last, count = active.get(symbol, (day, day, 0))
            active[symbol] = first, day, count + 1
    output.extend(_streak_record(symbol, active[symbol]) for symbol in sorted(active))
    return sorted(output, key=lambda item: (str(item["symbol"]), str(item["start_date"])))


def _streak_record(symbol: str, values: tuple[str, str, int]) -> dict[str, object]:
    return {"symbol": symbol, "start_date": values[0], "end_date": values[1], "session_count": values[2]}
