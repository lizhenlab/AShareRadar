"""Deterministic, research-only proposed budgets; never an order or a replay."""

from __future__ import annotations

from collections.abc import Mapping

from app.services.market_scan_allocation_budget import (
    AllocationBudget, allocation_nav, industry_values, initial_allocation_budget, prepare_allocation_limits,
    propose_allocation_orders, validate_final_allocation,
)
from app.services.market_scan_allocation_contracts import allocation_digest
from app.services.market_scan_allocation_validation import admit_allocation_inputs
from app.services.paper_trading_costs import resolve_cost_profile


def plan_market_scan_allocation(
    payload: object, policy_payload: object, *, expected_source_digests: Mapping[str, str], expected_policy_digest: str,
) -> dict[str, object]:
    """Bind independent source pins before planning conditional, cash-funded buys."""
    inputs, policy, input_digest, pins, captured_policy = admit_allocation_inputs(
        payload, policy_payload, expected_source_digests=expected_source_digests, expected_policy_digest=expected_policy_digest,
    )
    state = initial_allocation_budget(inputs, policy)
    before = allocation_exposure_snapshot(state)
    prepare_allocation_limits(state, inputs, policy)
    propose_allocation_orders(state, inputs, policy)
    validate_final_allocation(state, policy)
    report: dict[str, object] = {
        "schema_version": "market-scan-allocation-plan-v1", "status": "blocked" if state.blocking else "planned" if state.orders else "no_orders",
        "decision_at": inputs.decision_at, "input_digest": input_digest, "policy_digest": expected_policy_digest,
        "source_digests": pins, "policy": captured_policy,
        "cost_profile": resolve_cost_profile(policy.cost_profile).model_dump(mode="json"),
        "before": before, "after": allocation_exposure_snapshot(state), "proposed_orders": state.orders, "candidate_decisions": state.decisions,
        "blocking_reasons": sorted(set(state.blocking)), "fixed_candidate_slot_count": policy.top_n,
        "reserved_fee_upper_bound": sum(state.fee_reserves.values()) / 100,
        "conservative_nav_floor": state.nav_floor / 100 if state.nav_floor is not None else None,
        "promotion_eligible": False, "replay_integrated": False, "orders_submitted": False,
        "provenance": {"authority": "independently_pinned_self_asserted_snapshots", "official_pit_verified": False},
        "limitations": allocation_limitations(),
    }
    report["result_digest"] = allocation_digest(report)
    return report


def allocation_exposure_snapshot(state: AllocationBudget) -> dict[str, object]:
    nav = allocation_nav(state)
    values = list(state.values.values())
    complete = all(value is not None for value in values)
    market_value = sum(value for value in values if value is not None)
    return {
        "cash": state.cash / 100, "nav": nav / 100 if nav is not None else None,
        "cash_weight": _weight(state.cash, nav), "market_value": market_value / 100 if complete else None,
        "known_market_value": market_value / 100,
        "symbol_exposures": [{"symbol": symbol, "quantity": state.quantities[symbol], "industry": state.industries[symbol],
                              "market_value": value / 100 if value is not None else None, "nav_weight": _weight(value, nav)}
                             for symbol, value in sorted(state.values.items())],
        "industry_exposures": [{"industry": industry, "market_value": value / 100 if value is not None else None,
                                "nav_weight": _weight(value, nav)}
                               for industry, value in sorted(industry_values(state).items(), key=lambda item: (item[0] is None, item[0] or ""))],
    }


def _weight(value: int | None, nav: int | None) -> float | None:
    return value / nav if value is not None and nav is not None and nav > 0 else None


def allocation_limitations() -> list[str]:
    return [
        "proposed research budgets only; no replay integration, live orders, forced sales or model promotion",
        "rank is ordinal preference, not expected return; no covariance or correlation model is inferred",
        "caps are conditional on declared current valuation, classifications and estimated cost, not future fills or prices",
        "all current sleeves consume whole-account limits; unknown current risk or an existing breach blocks every new order",
        "fixed rank slots never refill or redistribute rejected budgets; unused amounts stay cash",
        "all possible slot fees are reserved before any buy so later fees cannot breach an earlier planned weight",
        "source pins bind selected snapshot bytes semantically, not official provenance or externally certified completeness",
        "prior-session turnover is a declared capacity scenario; it does not establish opening-auction or order-book liquidity",
        "held quantities are marked together per symbol; proposed order values use settled gross currency amounts",
    ]
