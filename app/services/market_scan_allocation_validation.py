"""Source pinning and explicitly bounded predecision information admission."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta

from app.artifacts.io import decode_json_bytes
from app.services.market_scan_allocation_contracts import (
    AllocationInput, AllocationInstrument, AllocationPolicy, allocation_date, allocation_digest,
    allocation_json_bytes, allocation_source_digests, allocation_time,
)
from app.services.paper_trading_costs import resolve_cost_profile
from app.services.trading_calendar import is_trading_day, previous_trade_date
from app.utils.clock import ASHARE_TIMEZONE


def admit_allocation_inputs(
    payload: object, policy_payload: object, *, expected_source_digests: Mapping[str, str], expected_policy_digest: str,
) -> tuple[AllocationInput, AllocationPolicy, str, dict[str, str], object]:
    payload = decode_json_bytes(allocation_json_bytes(payload))
    policy_payload = decode_json_bytes(allocation_json_bytes(policy_payload))
    pins = dict(expected_source_digests)
    if allocation_source_digests(payload) != pins:
        raise ValueError("independent allocation source pin mismatch")
    if allocation_digest(policy_payload) != expected_policy_digest:
        raise ValueError("independent allocation policy pin mismatch")
    inputs = AllocationInput.model_validate_json(allocation_json_bytes(payload), strict=True)
    policy = AllocationPolicy.model_validate_json(allocation_json_bytes(policy_payload), strict=True)
    decision = allocation_time(inputs.decision_at)
    cost = resolve_cost_profile(policy.cost_profile)
    if cost.profile_id != policy.cost_profile_id or decision.date().isoformat() < cost.effective_from:
        raise ValueError("allocation cost profile identity or effective date mismatch")
    if not is_trading_day(decision.date()) or len(inputs.candidates.rows) != policy.top_n:
        raise ValueError("allocation requires a trusted decision session and complete fixed candidate slots")
    for source in (inputs.account, inputs.candidates, inputs.market):
        if allocation_age_reason(source.observed_as_of, decision, policy.max_input_age_seconds):
            raise ValueError("allocation source snapshot is future or stale at decision")
    _validate_market_context(inputs, decision)
    return inputs, policy, allocation_digest(payload), pins, policy_payload


def allocation_age_reason(value: str | None, decision: datetime, max_seconds: int) -> str | None:
    if value is None:
        return "unavailable"
    age = (decision - allocation_time(value)).total_seconds()
    return "future" if age < 0 else "stale" if age > max_seconds else None


def _validate_market_context(inputs: AllocationInput, decision: datetime) -> None:
    expected = {row.symbol for row in inputs.account.positions} | {row.symbol for row in inputs.candidates.rows}
    source_time = allocation_time(inputs.market.observed_as_of)
    for row in inputs.market.rows:
        if row.symbol not in expected:
            raise ValueError("market source contains symbols outside the declared account and candidate union")
        for value in (row.valuation_observed_at, row.classification_observed_at, row.capacity_observed_at):
            if value is not None and allocation_time(value) > min(decision, source_time):
                raise ValueError("instrument evidence was observed after its source snapshot or decision")
        _validate_classification_dates(row)


def _validate_classification_dates(row: AllocationInstrument) -> None:
    values = row.classification_effective_from, row.classification_effective_through
    parsed = [allocation_date(value) for value in values if value is not None]
    if len(parsed) == 2 and parsed[0] > parsed[1]:
        raise ValueError("classification effective interval is reversed")
    if row.capacity_session_date is not None:
        allocation_date(row.capacity_session_date)


def valuation_reason(row: AllocationInstrument | None, decision: datetime, policy: AllocationPolicy) -> str | None:
    if row is None or row.price is None:
        return "valuation_unavailable"
    reason = allocation_age_reason(row.valuation_observed_at, decision, policy.max_quote_age_seconds)
    return "valuation_" + reason if reason else None


def classification_reason(row: AllocationInstrument | None, decision: datetime, policy: AllocationPolicy) -> str | None:
    if row is None or row.industry is None:
        return "classification_unavailable"
    reason = allocation_age_reason(row.classification_observed_at, decision, policy.max_classification_age_days * 86400)
    if reason:
        return "classification_" + reason
    start, end = row.classification_effective_from, row.classification_effective_through
    if start is None or end is None or not allocation_date(start) <= decision.date() <= allocation_date(end):
        return "classification_not_effective"
    return None


def capacity_reason(row: AllocationInstrument, decision: datetime) -> str | None:
    if row.capacity_amount is None or row.capacity_session_date is None or row.capacity_observed_at is None:
        return "prior_capacity_unavailable"
    previous = previous_trade_date(decision.date() - timedelta(days=1))
    if allocation_date(row.capacity_session_date) != previous:
        return "capacity_not_prior_session"
    if allocation_time(row.capacity_observed_at) < datetime.combine(previous, time(15), ASHARE_TIMEZONE):
        return "capacity_observed_before_reference_close"
    return None
