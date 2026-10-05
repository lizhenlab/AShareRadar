"""Retrospective, paired net-scenario evidence; never authorizes adoption.

Calendar anchors are frozen before inspecting returns. Missing mature anchors
invalidate inference rather than shortening the time axis or selecting survivors.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
from itertools import combinations
import json
import math
import random
from typing import cast

from app.services.strategy_template_tracking_metrics import STRATEGY_TEMPLATE_TRACKING_IDS
from app.services.trading_calendar import (
    TradingCalendarCoverageError, latest_expected_daily_kline_date, trading_dates_between,
)


SELECTION_SCHEMA_VERSION = "strategy-template-selection-v1"
MINIMUM_COMPLETE_ANCHORS = 20
BOOTSTRAP_SAMPLES = 2000
BLOCK_LENGTH_ANCHORS = 2
FAMILYWISE_ALPHA = 0.05
MAXIMUM_DRAWDOWN_DETERIORATION = 0.02
_ADOPTION_BLOCKERS = (
    "retrospective_template_selection", "independent_prospective_validation_missing",
    "continuous_capital_and_position_validation_missing",
)
_IDS = STRATEGY_TEMPLATE_TRACKING_IDS
_PAIRS = tuple(combinations(range(len(_IDS)), 2))


@dataclass(frozen=True)
class _Outcome:
    net_return: float
    stress_net_return: float
    drawdown: float


def build_strategy_template_selection_report(
    net_comparison: Mapping[str, object], *, run_failures: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Compare three fixed templates on complete nonoverlapping calendar anchors."""
    horizon, completed = _validate_contract(net_comparison)
    failures = _records(run_failures)
    groups, dates = _group_inputs(net_comparison, failures)
    blockers = _global_blockers(net_comparison, failures)
    cohorts = [
        _cohort_report(key, groups.get(key, ()), days, horizon, completed, blockers, net_comparison)
        for key, days in sorted(dates.items())
    ]
    identity = _digest({"net_comparison": net_comparison, "run_failures": list(failures)})
    available = any(item["status"] == "historical_evidence_available" for item in cohorts)
    return {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "status": "historical_evidence_available" if available else "insufficient_data",
        "evaluation_kind": "retrospective-net-scenario-selection", "evidence_digest": identity,
        "horizon_sessions": horizon, "as_of_completed_date": completed.isoformat(),
        "template_ids": list(_IDS), "adoptable_template_id": None, "promotion_eligible": False,
        "inference_contract": strategy_template_selection_contract(horizon), "cohorts": cohorts,
        "adoption_blockers": [*blockers, *_ADOPTION_BLOCKERS],
        "limitations": [
            "历史净场景的均值领先或统计优势，不等于已证明未来收益更好。",
            "每个评分合同单独比较；不跨合同挑选或合并赢家。",
            "单批次回撤并非连续账户净值回撤；各批次资金独立且现金不计利息。",
            "20 个完整锚点是项目的保守样本门槛，不是论文保证或充分经济验证。",
            "重复查看、改模板或改变日期范围后的显著性不构成独立事前验证。",
        ],
    }


def strategy_template_selection_contract(horizon: int) -> dict[str, object]:
    """Return the fixed inference identity for registration and retrospective reports."""
    if type(horizon) is not int or horizon not in (1, 5, 10, 20):
        raise ValueError("invalid strategy selection horizon")
    return {
        "anchor_policy": "first-contract-signal-date-then-every-H+1-trading-sessions",
        "anchor_spacing_sessions": horizon + 1, "minimum_complete_anchors": MINIMUM_COMPLETE_ANCHORS,
        "missing_mature_anchor_policy": "invalidate-all-inference-without-time-compression",
        "method": "two-sided-null-centered-circular-moving-block-bootstrap",
        "block_length_anchors": BLOCK_LENGTH_ANCHORS, "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "resampling_unit": "synchronized-three-template-calendar-anchor",
        "family": "all-three-pairwise-two-sided-comparisons", "family_size": len(_PAIRS),
        "multiplicity_adjustment": "Holm-FWER", "familywise_alpha": FAMILYWISE_ALPHA,
        "seed_policy": "sha256-of-frozen-contract-calendar-cost-and-execution-identities",
        "return_target": "D+1-open-to-D+H+1-close-independent-batch-net-return",
        "risk_target": "worst-independent-batch-drawdown",
        "maximum_drawdown_deterioration": MAXIMUM_DRAWDOWN_DETERIORATION,
        "winner_policy": "positive-paired-difference-and-adjusted-p-below-alpha-against-both-peers",
        "cost_stress_policy": "positive-paired-stress-difference-against-both-peers",
    }


def _validate_contract(report: Mapping[str, object]) -> tuple[int, date]:
    if report.get("schema_version") != "strategy-template-net-returns-v1":
        raise ValueError("unsupported strategy net comparison schema")
    horizon = report.get("horizon_sessions")
    if type(horizon) is not int or horizon not in (1, 5, 10, 20):
        raise ValueError("invalid strategy selection horizon")
    if report.get("entry_policy") != "D+1-open" or report.get("exit_policy") != "D+H+1-close":
        raise ValueError("strategy selection requires the fixed execution horizon")
    if report.get("continuous_portfolio_simulation") is not False or report.get("promotion_eligible") is not False:
        raise ValueError("strategy selection requires an independent retrospective scenario")
    _validate_template_cost_contract(report)
    return horizon, _validate_observation_cutoff(report)


def _validate_template_cost_contract(report: Mapping[str, object]) -> None:
    if report.get("return_unit") != "decimal_fraction" or not 10_000 <= _number(report.get("notional_cash_cny")) <= 1_000_000_000:
        raise ValueError("invalid strategy selection return unit or capital budget")
    specs = _records(report.get("cost_specs"))
    if sorted(str(item.get("template_id")) for item in specs) != sorted(_IDS):
        raise ValueError("strategy selection requires three fixed cost specifications")
    for item in specs:
        _hash(item.get("strategy_fingerprint"))


def _validate_observation_cutoff(report: Mapping[str, object]) -> date:
    observed = datetime.fromisoformat(str(report.get("as_of", "")))
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("strategy selection observation time requires a timezone")
    completed = _date(report.get("as_of_completed_date"))
    if latest_expected_daily_kline_date(observed, allow_auto_refresh=False) != completed:
        raise ValueError("strategy selection completed date disagrees with observation time")
    return completed


def _group_inputs(
    report: Mapping[str, object], failures: Sequence[Mapping[str, object]],
) -> tuple[dict[tuple[str, str], list[Mapping[str, object]]], dict[tuple[str, str], set[date]]]:
    groups: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    days: dict[tuple[str, str], set[date]] = {}
    run_ids: set[int] = set()
    for cohort in _records(report.get("cohorts")):
        key = _contract_key(cohort)
        if key in groups:
            raise ValueError("duplicate strategy selection cohort")
        groups[key] = list(_records(cohort.get("sessions")))
        days[key] = set()
        for session in groups[key]:
            day = _date(session.get("signal_date"))
            run_id = session.get("run_id")
            if type(run_id) is not int or run_id <= 0 or run_id in run_ids or day in days[key]:
                raise ValueError("duplicate or invalid strategy selection session")
            run_ids.add(run_id)
            days[key].add(day)
    for failure in failures:
        if _failure_has_contract(failure):
            key = _contract_key(failure)
            failed_day = _date(failure.get("signal_date"))
            days.setdefault(key, set()).add(failed_day)
            groups[key] = [row for row in groups.get(key, ()) if _date(row.get("signal_date")) != failed_day]
    return groups, {key: value for key, value in days.items() if value}


def _failure_has_contract(failure: Mapping[str, object]) -> bool:
    return all(failure.get(field) is not None for field in ("rule_version", "score_spec_hash", "signal_date"))


def _global_blockers(report: Mapping[str, object], failures: Sequence[Mapping[str, object]]) -> list[str]:
    result: list[str] = []
    evidence = _mapping(report.get("execution_evidence"))
    if evidence.get("provenance_status") != "official_raw_file_verified":
        result.append("official_execution_evidence_unavailable")
    if any(not _failure_has_contract(item) for item in failures):
        result.append("failed_source_contract_unresolved")
    return result


def _cohort_report(
    key: tuple[str, str], sessions: Sequence[Mapping[str, object]], signal_days: set[date],
    horizon: int, completed: date, global_blockers: Sequence[str], report: Mapping[str, object],
) -> dict[str, object]:
    calendar, anchors, mature, pending, calendar_blockers = _anchor_calendar(signal_days, horizon, completed)
    lookup = {_date(item.get("signal_date")): item for item in sessions}
    rows, missing = _anchor_outcomes(mature, calendar, lookup, horizon)
    blockers = [*global_blockers, *calendar_blockers]
    if missing:
        blockers.append("mature_calendar_anchors_missing")
    if len(rows) < MINIMUM_COMPLETE_ANCHORS:
        blockers.append("minimum_complete_anchors_not_met")
    contract_digest = _digest({
        "schema": SELECTION_SCHEMA_VERSION, "rule_version": key[0], "score_spec_hash": key[1],
        "anchors": [item.isoformat() for item in anchors], "horizon": horizon,
        "notional_cash_cny": report["notional_cash_cny"],
        "cost_specs": report["cost_specs"], "execution_evidence": report["execution_evidence"],
    })
    summaries = _template_summaries(rows)
    comparisons = _comparisons(rows, infer=not blockers, seed_text=contract_digest)
    leader = _leader(summaries) if rows and not missing and not global_blockers and not calendar_blockers else None
    winner = _statistical_winner(comparisons) if not blockers else None
    risk_winner, winner_blockers = _risk_winner(winner, summaries)
    return {
        "rule_version": key[0], "score_spec_hash": key[1], "contract_digest": contract_digest,
        "status": "insufficient_data" if blockers else "historical_evidence_available",
        "planned_anchor_dates": [item.isoformat() for item in anchors],
        "matured_anchor_dates": [item.isoformat() for item in mature],
        "pending_anchor_dates": [item.isoformat() for item in pending],
        "missing_anchor_dates": [item.isoformat() for item in missing], "complete_anchor_count": len(rows),
        "diagnostic_leader_id": leader, "statistical_winner_id": winner,
        "risk_qualified_winner_id": risk_winner, "adoptable_template_id": None,
        "template_summaries": summaries, "comparisons": comparisons,
        "blockers": [*blockers, *winner_blockers], "adoption_blockers": list(_ADOPTION_BLOCKERS),
    }


def _anchor_calendar(
    signal_days: set[date], horizon: int, completed: date,
) -> tuple[tuple[date, ...], tuple[date, ...], tuple[date, ...], tuple[date, ...], list[str]]:
    first, last = min(signal_days), max(signal_days)
    if last > completed or (completed - first).days > 36_600:
        raise ValueError("invalid or unbounded strategy selection date window")
    try:
        calendar = trading_dates_between(first, completed, allow_auto_refresh=False)
    except TradingCalendarCoverageError:
        return (), (), (), (), ["trusted_calendar_unavailable"]
    if not signal_days.issubset(calendar):
        raise ValueError("strategy signal date is not a trusted trading session")
    signals = tuple(item for item in calendar if item <= last)
    anchors = signals[::horizon + 1]
    positions = {day: index for index, day in enumerate(calendar)}
    mature = tuple(day for day in anchors if positions[day] + horizon + 1 < len(calendar))
    pending = tuple(day for day in anchors if day not in mature)
    return calendar, anchors, mature, pending, []


def _anchor_outcomes(
    anchors: Sequence[date], calendar: Sequence[date], sessions: Mapping[date, Mapping[str, object]], horizon: int,
) -> tuple[list[tuple[_Outcome, ...]], list[date]]:
    rows: list[tuple[_Outcome, ...]] = []
    missing: list[date] = []
    positions = {day: index for index, day in enumerate(calendar)}
    for day in anchors:
        session = sessions.get(day)
        if session is None:
            missing.append(day)
            continue
        index = positions[day]
        if _date(session.get("entry_date")) != calendar[index + 1] or _date(session.get("exit_date")) != calendar[index + horizon + 1]:
            raise ValueError("strategy net outcome execution dates disagree with the calendar")
        selections = _records(session.get("selections"))
        if sorted(str(item.get("template_id")) for item in selections) != sorted(_IDS):
            raise ValueError("strategy anchor must include the fixed three templates")
        by_id = {str(item["template_id"]): _outcome(item) for item in selections}
        if any(by_id[item] is None for item in _IDS):
            missing.append(day)
        else:
            rows.append(tuple(cast(_Outcome, by_id[item]) for item in _IDS))
    return rows, missing


def _outcome(selection: Mapping[str, object]) -> _Outcome | None:
    status = selection.get("status")
    if status not in ("available", "pending", "unavailable", "blocked"):
        raise ValueError("unknown strategy net outcome status")
    if status != "available":
        return None
    base, stress = _mapping(selection.get("base")), _mapping(selection.get("stress"))
    if base.get("status") != "available" or stress.get("status") != "available":
        raise ValueError("available strategy outcome requires both cost scenarios")
    value, stressed = _number(selection.get("net_return")), _number(selection.get("stress_net_return"))
    if value < -1 or stressed < -1:
        raise ValueError("strategy net return exceeds the funded loss bound")
    if value != _number(base.get("net_return")) or stressed != _number(stress.get("net_return")):
        raise ValueError("strategy net outcome aliases disagree")
    drawdowns = [_number(item.get("independent_batch_max_drawdown")) for item in (base, stress)]
    if any(not -1 <= item <= 0 for item in drawdowns):
        raise ValueError("invalid independent batch drawdown")
    return _Outcome(value, stressed, min(drawdowns))


def _template_summaries(rows: Sequence[tuple[_Outcome, ...]]) -> list[dict[str, object]]:
    return [{
        "template_id": template, "complete_anchor_count": len(rows),
        "mean_net_return": _mean([row[index].net_return for row in rows]) if rows else None,
        "mean_stress_net_return": _mean([row[index].stress_net_return for row in rows]) if rows else None,
        "worst_independent_batch_drawdown": min(row[index].drawdown for row in rows) if rows else None,
    } for index, template in enumerate(_IDS)]


def _comparisons(rows: Sequence[tuple[_Outcome, ...]], *, infer: bool, seed_text: str) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for left, right in _PAIRS:
        differences = [row[left].net_return - row[right].net_return for row in rows]
        stress = [row[left].stress_net_return - row[right].stress_net_return for row in rows]
        results.append({
            "left_template_id": _IDS[left], "right_template_id": _IDS[right],
            "mean_net_return_difference": _mean(differences) if rows else None,
            "mean_stress_net_return_difference": _mean(stress) if rows else None,
            "two_sided_p_value": _two_sided_p_value(differences, seed_text) if infer else None,
            "holm_adjusted_p_value": None, "reject_equal_means": None,
        })
    if infer:
        _adjust_holm(results)
    return results


def _two_sided_p_value(values: Sequence[float], seed_text: str) -> float:
    scale = max(abs(value) for value in values)
    if scale == 0:
        return 1.0
    scaled = [value / scale for value in values]
    observed = _mean(scaled)
    centered = [value - observed for value in scaled]
    generator = random.Random(int.from_bytes(hashlib.sha256(seed_text.encode()).digest()[:8], "big"))
    exceedances = 0
    for _ in range(BOOTSTRAP_SAMPLES):
        resampled: list[float] = []
        while len(resampled) < len(values):
            start = generator.randrange(len(values))
            resampled.extend(centered[(start + offset) % len(values)] for offset in range(BLOCK_LENGTH_ANCHORS))
        exceedances += abs(_mean(resampled[:len(values)])) >= abs(observed) - 1e-15
    return (exceedances + 1) / (BOOTSTRAP_SAMPLES + 1)


def _adjust_holm(comparisons: list[dict[str, object]]) -> None:
    ordered = sorted(enumerate(comparisons), key=lambda pair: _number(pair[1]["two_sided_p_value"]))
    previous = 0.0
    for rank, (index, item) in enumerate(ordered):
        adjusted = min(1.0, max(previous, (len(_PAIRS) - rank) * _number(item["two_sided_p_value"])))
        previous = adjusted
        comparisons[index]["holm_adjusted_p_value"] = adjusted
        comparisons[index]["reject_equal_means"] = adjusted < FAMILYWISE_ALPHA


def _leader(summaries: Sequence[Mapping[str, object]]) -> str | None:
    ordered = sorted(summaries, key=lambda item: _number(item["mean_net_return"]), reverse=True)
    if len(ordered) < 2 or ordered[0]["mean_net_return"] == ordered[1]["mean_net_return"]:
        return None
    return str(ordered[0]["template_id"])


def _statistical_winner(comparisons: Sequence[Mapping[str, object]]) -> str | None:
    wins = {template: 0 for template in _IDS}
    for item in comparisons:
        if item["reject_equal_means"] is not True:
            continue
        difference = _number(item["mean_net_return_difference"])
        if difference != 0:
            winner = item["left_template_id"] if difference > 0 else item["right_template_id"]
            wins[str(winner)] += 1
    return next((template for template, count in wins.items() if count == 2), None)


def _risk_winner(winner: str | None, summaries: Sequence[Mapping[str, object]]) -> tuple[str | None, list[str]]:
    if winner is None:
        return None, ["no_familywise_statistical_winner"]
    chosen = next(item for item in summaries if item["template_id"] == winner)
    others = [item for item in summaries if item["template_id"] != winner]
    blockers: list[str] = []
    if any(_number(chosen["mean_stress_net_return"]) <= _number(item["mean_stress_net_return"]) for item in others):
        blockers.append("cost_stress_advantage_not_confirmed")
    if any(_number(chosen["worst_independent_batch_drawdown"]) + MAXIMUM_DRAWDOWN_DETERIORATION
           < _number(item["worst_independent_batch_drawdown"]) - 1e-12 for item in others):
        blockers.append("independent_batch_drawdown_limit_exceeded")
    return (None if blockers else winner), blockers


def _mean(values: Sequence[float]) -> float:
    scale = max(abs(value) for value in values)
    return scale * (math.fsum(value / scale for value in values) / len(values)) if scale else 0.0


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("strategy selection requires finite numeric evidence")
    try:
        parsed = float(value)
    except OverflowError as exc:
        raise ValueError("strategy selection requires finite numeric evidence") from exc
    if not math.isfinite(parsed):
        raise ValueError("strategy selection requires finite numeric evidence")
    return parsed


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("strategy selection requires canonical calendar dates")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("strategy selection requires canonical calendar dates")
    return parsed


def _hash(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("strategy selection requires a verified identity")
    return value


def _contract_key(item: Mapping[str, object]) -> tuple[str, str]:
    rule = item.get("rule_version")
    if not isinstance(rule, str) or not rule or len(rule) > 128:
        raise ValueError("invalid strategy selection score contract")
    return rule, _hash(item.get("score_spec_hash"))


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError("invalid strategy selection evidence structure")
    return value


def _records(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 10_000:
        raise ValueError("invalid or unbounded strategy selection evidence collection")
    return tuple(_mapping(item) for item in value)


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("strategy selection evidence is not finite canonical JSON") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()
