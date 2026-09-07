"""Whole-account integer-cent budgets with a conservative common NAV floor."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.models.paper_trading import PaperCostProfile
from app.services.market_scan_allocation_contracts import (
    AllocationCandidate, AllocationInput, AllocationInstrument, AllocationPolicy, allocation_cents, allocation_time,
)
from app.services.market_scan_allocation_validation import capacity_reason, classification_reason, valuation_reason
from app.services.market_scan_evaluation_execution import affordable_execution_purchase
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs


@dataclass
class AllocationBudget:
    cash: int
    quantities: dict[str, int] = field(default_factory=dict)
    values: dict[str, int | None] = field(default_factory=dict)
    industries: dict[str, str | None] = field(default_factory=dict)
    blocking: list[str] = field(default_factory=list)
    orders: list[dict[str, object]] = field(default_factory=list)
    decisions: list[dict[str, object]] = field(default_factory=list)
    slot_cash: int = 0
    fee_reserves: dict[str, int] = field(default_factory=dict)
    nav_floor: int | None = None


def initial_allocation_budget(inputs: AllocationInput, policy: AllocationPolicy) -> AllocationBudget:
    state = AllocationBudget(allocation_cents(inputs.account.cash))
    instruments = {row.symbol: row for row in inputs.market.rows}
    decision = allocation_time(inputs.decision_at)
    for holding in inputs.account.positions:
        state.quantities[holding.symbol] = state.quantities.get(holding.symbol, 0) + holding.quantity
    for symbol, quantity in state.quantities.items():
        row = instruments.get(symbol)
        valuation = valuation_reason(row, decision, policy)
        classification = classification_reason(row, decision, policy)
        state.blocking.extend(f"current:{symbol}:{reason}" for reason in (valuation, classification) if reason)
        state.values[symbol] = _marked_cents(row, quantity) if valuation is None else None
        state.industries[symbol] = row.industry if row is not None and classification is None else None
    return state


def _marked_cents(row: AllocationInstrument | None, quantity: int) -> int:
    assert row is not None and row.price is not None
    return allocation_cents(round(float(Decimal(str(row.price)) * quantity), 2))


def allocation_nav(state: AllocationBudget) -> int | None:
    if any(value is None for value in state.values.values()):
        return None
    total = state.cash + sum(value for value in state.values.values() if value is not None)
    allocation_cents(total / 100)
    return total


def industry_values(state: AllocationBudget) -> dict[str | None, int | None]:
    result: dict[str | None, int | None] = {}
    for symbol, value in state.values.items():
        industry = state.industries[symbol]
        prior = result.get(industry, 0)
        result[industry] = prior + value if prior is not None and value is not None else None
    return result


def weight_limit_cents(nav: int, weight: float) -> int:
    return int(Decimal(nav) * Decimal(str(weight)))


def prepare_allocation_limits(state: AllocationBudget, inputs: AllocationInput, policy: AllocationPolicy) -> None:
    nav = allocation_nav(state)
    state.slot_cash = state.cash // policy.top_n
    instruments = {row.symbol: row for row in inputs.market.rows}
    state.fee_reserves = {candidate.symbol: _candidate_fee_reserve(state.slot_cash, instruments.get(candidate.symbol), inputs, policy)
                          for candidate in inputs.candidates.rows}
    if nav is None or nav <= 0:
        state.blocking.append("account_nav_unavailable_or_nonpositive")
        return
    state.nav_floor = nav - sum(state.fee_reserves.values())
    if state.nav_floor <= 0:
        state.blocking.append("fee_reserve_exceeds_account_nav")
    elif _risk_limit_breached(state, policy, nav):
        state.blocking.append("existing_risk_limit_breach")
    elif _risk_limit_breached(state, policy, state.nav_floor):
        state.blocking.append("existing_risk_limit_after_fee_reserve")


def _candidate_fee_reserve(cash: int, row: AllocationInstrument | None, inputs: AllocationInput, policy: AllocationPolicy) -> int:
    if _candidate_reason(row, inputs, policy):
        return 0
    assert row is not None and row.price is not None
    profile = resolve_cost_profile(policy.cost_profile)
    gross_limit = min(_affordable_gross_cents(cash, profile), _capacity_cents(row, policy))
    _, _, fees = affordable_execution_purchase(
        cash / 100, row.price, row.minimum_buy_quantity, row.buy_quantity_step,
        profile, gross_limit=gross_limit / 100,
    )
    return allocation_cents(fees)


def _affordable_gross_cents(cash: int, profile: PaperCostProfile) -> int:
    # Bound the existing lot helper without a cash-sized decrement loop.
    lower, upper = 0, cash
    while lower < upper:
        gross = (lower + upper + 1) // 2
        fee = allocation_cents(trade_costs(profile, side="buy", gross_amount=gross / 100).total)
        if gross + fee <= cash:
            lower = gross
        else:
            upper = gross - 1
    return lower


def _capacity_cents(row: AllocationInstrument, policy: AllocationPolicy) -> int:
    assert row.capacity_amount is not None
    return int(Decimal(str(row.capacity_amount)) * Decimal(str(policy.max_participation_rate)) * 100)


def _risk_limit_breached(state: AllocationBudget, policy: AllocationPolicy, nav: int) -> bool:
    symbol_limit = weight_limit_cents(nav, policy.max_symbol_weight)
    industry_limit = weight_limit_cents(nav, policy.max_industry_weight)
    return any(value is not None and value > symbol_limit for value in state.values.values()) or any(
        value is not None and value > industry_limit for value in industry_values(state).values()
    )


def propose_allocation_orders(state: AllocationBudget, inputs: AllocationInput, policy: AllocationPolicy) -> None:
    instruments = {row.symbol: row for row in inputs.market.rows}
    for candidate in sorted(inputs.candidates.rows, key=lambda row: row.frozen_rank):
        reason = "account_blocked" if state.blocking else _candidate_reason(instruments.get(candidate.symbol), inputs, policy)
        if reason:
            _reject_candidate(state, candidate, reason)
        else:
            _propose_candidate(state, candidate, instruments[candidate.symbol], policy)


def _candidate_reason(row: AllocationInstrument | None, inputs: AllocationInput, policy: AllocationPolicy) -> str | None:
    decision = allocation_time(inputs.decision_at)
    reason = valuation_reason(row, decision, policy) or classification_reason(row, decision, policy)
    if reason:
        return reason
    assert row is not None
    return capacity_reason(row, decision)


def _reject_candidate(state: AllocationBudget, candidate: AllocationCandidate, reason: str) -> None:
    state.decisions.append({"symbol": candidate.symbol, "frozen_rank": candidate.frozen_rank,
                            "status": "rejected", "reason": reason, "slot_budget": state.slot_cash / 100})


def _propose_candidate(state: AllocationBudget, candidate: AllocationCandidate, row: AllocationInstrument, policy: AllocationPolicy) -> None:
    assert row.price is not None and row.industry is not None and row.capacity_amount is not None and state.nav_floor is not None
    limit = _candidate_gross_limit(state, row, policy)
    quantity, gross, fees = affordable_execution_purchase(
        state.slot_cash / 100, row.price, row.minimum_buy_quantity, row.buy_quantity_step,
        resolve_cost_profile(policy.cost_profile), gross_limit=max(0, limit) / 100,
    )
    if not quantity:
        _reject_candidate(state, candidate, "cash_risk_or_prior_capacity_below_minimum_lot")
        return
    gross_cents, fee_cents = allocation_cents(gross), allocation_cents(fees)
    if fee_cents > state.fee_reserves[row.symbol] or gross_cents + fee_cents > min(state.slot_cash, state.cash):
        raise ValueError("allocation fee or cash reserve invariant failed")
    state.cash -= gross_cents + fee_cents
    state.values[row.symbol] = (state.values.get(row.symbol) or 0) + gross_cents
    state.quantities[row.symbol] = state.quantities.get(row.symbol, 0) + quantity
    state.industries[row.symbol] = row.industry
    order: dict[str, object] = {
        "symbol": row.symbol, "frozen_rank": candidate.frozen_rank, "side": "buy", "quantity": quantity,
        "planning_price": row.price, "gross_amount": gross, "estimated_fees": fees, "total_budget": (gross_cents + fee_cents) / 100,
        "slot_budget": state.slot_cash / 100, "gross_budget_limit": max(0, limit) / 100,
        "industry": row.industry, "capacity_session_date": row.capacity_session_date,
    }
    state.orders.append(order)
    state.decisions.append({**order, "status": "proposed", "reason": None})


def _candidate_gross_limit(state: AllocationBudget, row: AllocationInstrument, policy: AllocationPolicy) -> int:
    assert state.nav_floor is not None and row.capacity_amount is not None
    held = state.values.get(row.symbol) or 0
    sector = industry_values(state).get(row.industry) or 0
    reserve = state.fee_reserves[row.symbol]
    return min(state.slot_cash - reserve, state.cash - reserve, _capacity_cents(row, policy),
               weight_limit_cents(state.nav_floor, policy.max_symbol_weight) - held,
               weight_limit_cents(state.nav_floor, policy.max_industry_weight) - sector)


def validate_final_allocation(state: AllocationBudget, policy: AllocationPolicy) -> None:
    if state.blocking:
        return
    nav = allocation_nav(state)
    if nav is None or state.nav_floor is None or nav < state.nav_floor or _risk_limit_breached(state, policy, nav):
        raise ValueError("final allocation violates complete cost-adjusted account risk limits")
