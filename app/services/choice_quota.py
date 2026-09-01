"""Sanitized Choice quota queries and conservative local capacity planning."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.services.choice_research import DAILY_FIELDS, DIVIDEND_FIELDS, METADATA_FIELDS, REFERENCE_FIELDS
from app.services.choice_research_store import ChoiceBudget, quota_contract, quota_integer
from app.services.choice_sdk import ChoiceError
from app.utils.clock import market_now


QUERY_DAYS = 30
PUBLIC_FIELDS = (
    "FUNCENAME", "SECUTYPE", "PERIOD", "STARTDATE", "ENDDATE", "THRESHOLD",
    "USEDDATA", "AVAILABEDATA", "EFFECTIVEDATE",
)
PAID_FUNCTIONS = ("EM_CSD", "EM_CSS", "EM_CTR")
SUPPORTED_QUOTAS = {*PAID_FUNCTIONS, "EM_CFC"}


def quota_query_window(today: date | None = None) -> tuple[date, date]:
    """Return the inclusive 30-calendar-day range recommended by Choice."""
    end = today or market_now().date()
    return end - timedelta(days=QUERY_DAYS - 1), end


def quota_query_args(today: date | None = None) -> list[str]:
    start, end = quota_query_window(today)
    options = f"StartDate={start.isoformat()},EndDate={end.isoformat()},Ispandas=0"
    return ["", ",".join(PUBLIC_FIELDS), options]


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ChoiceError(f"invalid Choice quota {label}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ChoiceError(f"invalid Choice quota {label}") from exc
    if result < 0:
        raise ChoiceError(f"invalid negative Choice quota {label}")
    return result


def _public_quota(function: str, row: dict[str, Any]) -> dict[str, Any]:
    if row.get("FUNCENAME") != function or any(field not in row for field in PUBLIC_FIELDS):
        raise ChoiceError("Choice quota response is missing required public fields")
    for field in ("SECUTYPE", "PERIOD", "STARTDATE", "ENDDATE", "EFFECTIVEDATE"):
        if not isinstance(row[field], str) or not row[field]:
            raise ChoiceError("Choice quota response has an invalid public field")
    quota_contract(row)
    return {
        "FUNCENAME": function,
        "SECUTYPE": row["SECUTYPE"],
        "PERIOD": row["PERIOD"],
        "STARTDATE": row["STARTDATE"],
        "ENDDATE": row["ENDDATE"],
        "THRESHOLD": quota_integer(row, "THRESHOLD"),
        "EFFECTIVEDATE": row["EFFECTIVEDATE"],
        "USEDDATA": quota_integer(row, "USEDDATA"),
        "AVAILABEDATA": quota_integer(row, "AVAILABEDATA"),
    }


def public_quotas(quotas: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {function: _public_quota(function, quotas[function]) for function in sorted(SUPPORTED_QUOTAS & quotas.keys())}


def sanitized_quota_response(quotas: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return the minimal replayable account snapshot, excluding provider extras."""
    public = public_quotas(quotas)
    return {"error_code": 0, "indicators": list(PUBLIC_FIELDS), "data": {
        function: [row[field] for field in PUBLIC_FIELDS] for function, row in public.items()
    }}


def _reservations(budget: ChoiceBudget) -> list[dict[str, Any]]:
    if budget.db is None:
        raise ChoiceError("Choice quota report requires an active budget ledger")
    rows = budget.db.execute(
        "SELECT period,function,SUM(units) FROM reservations GROUP BY period,function ORDER BY period,function"
    )
    return [
        {"period": period, "function": function, "estimated_units": _nonnegative(units, "reservation")}
        for period, function, units in rows
    ]


def _weekly_period(today: date) -> str:
    start = today - timedelta(days=today.weekday())
    return f"{start.isoformat()}/{(start + timedelta(days=6)).isoformat()}"


def _function_capacity(function: str, quota: dict[str, Any] | None, limit: int,
                       totals: dict[tuple[str, str], int], today: date) -> dict[str, Any]:
    base: dict[str, Any] = {"function": function, "configured_local_limit": limit,
        "expected_current_period": _weekly_period(today), "provider_settlement_proven": False,
        "reservations_released": 0}
    if quota is None:
        return {**base, "reported_remaining": None, "current_period_reserved_units": 0,
                "reported_for_current_period": False, "safe_units": 0, "status": "paused_missing_quota"}
    start, end, effective = quota_contract(quota)
    period = f"{start.isoformat()}/{end.isoformat()}"
    reported_reserved = totals.get((period, function), 0)
    current_reserved = totals.get((_weekly_period(today), function), 0)
    base.update({"security_type": quota["SECUTYPE"], "period_type": quota["PERIOD"], "period": period,
        "reported_threshold": quota["THRESHOLD"], "effective_until": quota["EFFECTIVEDATE"],
        "reported_used": quota["USEDDATA"], "reported_remaining": quota["AVAILABEDATA"],
        "reported_period_reserved_units": reported_reserved, "current_period_reserved_units": current_reserved,
        "reported_for_current_period": start <= today <= end})
    if today > effective:
        return {**base, "safe_units": 0, "status": "paused_expired"}
    if today > end:
        return {**base, "safe_units": 0, "status": "paused_rollover_unconfirmed",
                "rollover_state": "current_period_row_missing"}
    if today < start:
        return {**base, "safe_units": 0, "status": "paused_outside_period"}
    safe = max(0, min(limit, quota["AVAILABEDATA"]) - current_reserved)
    return {**base, "safe_units": safe, "status": "available_guarded" if safe else "paused_no_safe_units"}


def _local_capacity(function: str, limit: int, totals: dict[tuple[str, str], int], today: date) -> dict[str, Any]:
    period = today.strftime("%G-W%V")
    reserved = totals.get((period, function), 0)
    safe = max(0, limit - reserved)
    return {"function": function, "period": period, "configured_local_limit": limit,
            "current_period_reserved_units": reserved, "safe_units": safe,
            "status": "local_guard_only_available" if safe else "local_guard_only_paused",
            "provider_quota_reported": False,
            "not_free_or_unlimited": True, "reservations_released": 0}


def _scenario(symbols: int, sessions: int, csd_safe: int, css_safe: int) -> dict[str, Any]:
    csd, css = symbols * sessions * len(DAILY_FIELDS), symbols * sessions * len(REFERENCE_FIELDS)
    return {"symbols": symbols, "sessions": sessions, "estimated_units": {"EM_CSD": csd, "EM_CSS": css},
            "feasible_under_current_safe_units": csd <= csd_safe and css <= css_safe}


def _cohort_scenario(symbols: int, sessions: int, snapshots: int, reports: int,
                     csd_safe: int, css_safe: int) -> dict[str, Any]:
    csd = symbols * sessions * len(DAILY_FIELDS)
    metadata = symbols * snapshots * len(METADATA_FIELDS)
    dividends = symbols * reports * len(DIVIDEND_FIELDS)
    references = symbols * (sessions - snapshots) * len(REFERENCE_FIELDS)
    css = metadata + dividends + references
    return {"symbols": symbols, "sessions": sessions,
        "estimated_units": {"EM_CSD": csd, "EM_CSS": css},
        "css_components": {"metadata": metadata, "dividend_snapshots": dividends,
                           "non_snapshot_execution_references": references},
        "feasible_under_current_safe_units": csd <= csd_safe and css <= css_safe,
        "requires_disjoint_sample_and_gap_review": True}


def _capacity_planning(functions: dict[str, dict[str, Any]], *, symbols: int, sessions: int,
                       snapshots: int, reports: int) -> dict[str, Any]:
    if (not 1 <= symbols <= 225 or not 1 <= sessions <= 800 or not 1 <= snapshots <= sessions
            or not 0 <= reports <= 20):
        raise ChoiceError("quota planning scope is outside the bounded research range")
    csd_safe, css_safe = functions["EM_CSD"]["safe_units"], functions["EM_CSS"]["safe_units"]
    paired_functions = ("EM_CSD", "EM_CSS")
    current = all(functions[name].get("reported_for_current_period") for name in paired_functions)
    available = all(functions[name].get("status") == "available_guarded" for name in paired_functions)
    per_symbol = {"EM_CSD": sessions * len(DAILY_FIELDS), "EM_CSS": sessions * len(REFERENCE_FIELDS)}
    per_session = {"EM_CSD": symbols * len(DAILY_FIELDS), "EM_CSS": symbols * len(REFERENCE_FIELDS)}
    minimum = _cohort_scenario(3, sessions, snapshots, reports, csd_safe, css_safe)
    if not current:
        status = "unavailable_without_current_period_quota"
    elif not available:
        status = "unavailable_current_period_functions_paused"
    elif minimum["feasible_under_current_safe_units"]:
        status = "arithmetic_candidates_require_gap_review"
    else:
        status = "insufficient_safe_units_for_minimum_balanced_cohort"
    return {
        "schema_version": "choice-capacity-planning-v2", "arithmetic_only_not_gap_or_authorization": True,
        "status": status, "current_period_quota_confirmed": current,
        "paired_functions_available": available,
        "assumptions": {"symbols": symbols, "sessions": sessions, "csd_fields": len(DAILY_FIELDS),
            "css_reference_fields": len(REFERENCE_FIELDS), "metadata_snapshot_dates": snapshots,
            "dividend_report_dates": reports, "rounding": "floor"},
        "paired_capacity": {
            "additional_symbols_for_assumed_sessions": min(csd_safe // per_symbol["EM_CSD"], css_safe // per_symbol["EM_CSS"]),
            "additional_sessions_for_assumed_symbols": min(csd_safe // per_session["EM_CSD"], css_safe // per_session["EM_CSS"]),
        },
        "reference_only_css_capacity": {
            "additional_symbols_for_assumed_sessions": css_safe // per_symbol["EM_CSS"],
            "additional_sessions_for_assumed_symbols": css_safe // per_session["EM_CSS"],
            "not_usable_as_paired_history_without_csd": True,
        },
        "examples": [_scenario(count, sessions, csd_safe, css_safe) for count in (12, 18, 30)],
        "complete_research_cohort_examples": [
            _cohort_scenario(count, sessions, snapshots, reports, csd_safe, css_safe) for count in (30, 60)
        ],
        "minimum_balanced_research_cohort": minimum,
        "extend_current_symbols_100_sessions": _scenario(symbols, 100, csd_safe, css_safe),
        "prohibitions": ["no_automatic_data_request", "no_reservation_release", "no_repeat_complete_scope",
                         "no_formal_pit_or_production_authority_claim"],
    }


def _action_plan(functions: dict[str, dict[str, Any]], capacity: dict[str, Any]) -> dict[str, Any]:
    paired_names = ("EM_CSD", "EM_CSS")
    paired_noncurrent = [name for name in paired_names if not functions[name].get("reported_for_current_period")]
    paired_unavailable = [name for name in paired_names if functions[name].get("status") != "available_guarded"]
    minimum_feasible = capacity["minimum_balanced_research_cohort"]["feasible_under_current_safe_units"]
    if paired_noncurrent:
        paired_status = "blocked_current_period_unreported"
        paired_steps = ["requery_datastatistics_later", "confirm_rollover_with_choice_support"]
    elif paired_unavailable or not minimum_feasible:
        paired_status = "blocked_no_paired_capacity"
        paired_steps = ["review_current_period_reservations_and_reported_remaining"]
    else:
        paired_status = "arithmetic_candidates_require_gap_review"
        paired_steps = ["lock_disjoint_sample_before_collection", "verify_exact_plan_cost_before_reservation"]

    ctr = functions["EM_CTR"]
    if not ctr.get("reported_for_current_period"):
        event_status = "blocked_current_period_unreported"
        event_steps = ["requery_datastatistics_later", "confirm_rollover_with_choice_support"]
    elif ctr.get("status") != "available_guarded":
        event_status = "blocked_function_unavailable"
        event_steps = ["review_current_period_reservations_and_reported_remaining"]
    else:
        event_status = "available_guarded_requires_exact_plan"
        event_steps = ["verify_exact_event_request_count_before_reservation"]

    noncurrent = [name for name in PAID_FUNCTIONS if not functions[name].get("reported_for_current_period")]
    return {"status": paired_status, "paid_functions_not_current": noncurrent,
            "workflows": {
                "paired_history": {"status": paired_status, "required_functions": list(paired_names),
                    "functions_not_current": paired_noncurrent, "functions_unavailable": paired_unavailable,
                    "minimum_balanced_cohort_feasible": minimum_feasible, "next_steps": paired_steps},
                "event_queries": {"status": event_status, "required_functions": ["EM_CTR"],
                    "functions_not_current": ["EM_CTR"] if not ctr.get("reported_for_current_period") else [],
                    "functions_unavailable": ["EM_CTR"] if ctr.get("status") != "available_guarded" else [],
                    "safe_requests": ctr["safe_units"], "next_steps": event_steps},
            }, "execute_automatically": False, "market_data_probe_permitted": False,
            "next_steps": paired_steps, "formal_pit_or_production_authority": False}


def _cfc_report(quota: dict[str, Any] | None, today: date) -> dict[str, Any]:
    current = False
    if quota:
        start, end, _ = quota_contract(quota)
        current = start <= today <= end
    return {"function": "EM_CFC", "security_type": quota["SECUTYPE"] if quota else None,
        "period_type": quota["PERIOD"] if quota else None,
        "period": f'{quota["STARTDATE"]}/{quota["ENDDATE"]}' if quota else None,
        "reported_threshold": quota["THRESHOLD"] if quota else None,
        "reported_remaining": quota["AVAILABEDATA"] if quota else None,
        "reported_for_current_period": current, "configured_local_limit": None,
        "current_period_reserved_units": 0, "safe_units": 0, "status": "unsupported_not_wired",
        "collector_supported": False, "reservations_released": 0}


def build_quota_report(budget: ChoiceBudget, *, queried_at: str, today: date,
                       planning_symbols: int, planning_sessions: int,
                       planning_snapshot_dates: int = 26, planning_report_dates: int = 11) -> dict[str, Any]:
    quotas = public_quotas(budget.quotas)
    reservations = _reservations(budget)
    totals = {(row["period"], row["function"]): row["estimated_units"] for row in reservations}
    functions = {name: _function_capacity(name, quotas.get(name), budget.limits[name], totals, today)
                 for name in PAID_FUNCTIONS}
    functions["EM_CFC"] = _cfc_report(quotas.get("EM_CFC"), today)
    start, end = quota_query_window(today)
    capacity = _capacity_planning(functions, symbols=planning_symbols, sessions=planning_sessions,
        snapshots=planning_snapshot_dates, reports=planning_report_dates)
    return {"schema_version": "choice-quota-report-v3", "queried_at": queried_at,
        "query_window": {"start_date": start.isoformat(), "end_date": end.isoformat(), "calendar_days": QUERY_DAYS},
        "quota": quotas, "functions": functions,
        "local_only_functions": {name: _local_capacity(name, budget.limits[name], totals, today)
                                 for name in ("sector", "tradedates")},
        "local_reservations": reservations, "capacity_planning": capacity,
        "action_plan": _action_plan(functions, capacity),
        "reservations_released": 0, "market_data_requests": 0,
        "warning": "provider statistics may lag; safe units subtract every local reservation and are not a billing guarantee"}
