"""Fixed-slot executable returns; unknown positions never become implicit cash.

This is a per-signal scenario, not a daily shared-capital portfolio ledger.
All frozen slots retain their equal weights, including unfilled cash slots.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
import math
from statistics import fmean
from typing import Literal

from app.models.paper_trading import CostProfileName, PaperCostProfile
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs


@dataclass(frozen=True)
class ExecutionOutcome:
    status: Literal["modelled", "unfilled", "data_unavailable"]
    reason: str
    gross_return: float | None = None
    net_return: float | None = None
    cost_drag: float | None = None
    entry_date: str | None = None
    exit_date: str | None = None
    exit_delay_sessions: int = 0
    model_limited: bool = False
    buy_amount: float | None = None
    sell_amount: float | None = None
    position_open: bool = False
    allocated_capital: float | None = None
    price_return: float | None = None


def execution_scenario_value(
    outcome: ExecutionOutcome | None, cost_profile: CostProfileName | None = None,
) -> float | None:
    if outcome is None or outcome.status == "data_unavailable" or outcome.position_open:
        return None
    if outcome.status == "unfilled":
        # Legacy delayed-exit outcomes must never be mistaken for an unfilled buy.
        return None if outcome.reason.startswith("exit_") else 0.0
    if cost_profile is None:
        value = outcome.net_return
    else:
        value = scenario_net_return(outcome, cost_profile)
    return value if value is not None and math.isfinite(value) else None


def scenario_net_return(outcome: ExecutionOutcome, cost_profile: CostProfileName) -> float | None:
    if outcome.buy_amount is None or outcome.sell_amount is None:
        return None
    profile = resolve_cost_profile(cost_profile)
    buy_cost = trade_costs(profile, side="buy", gross_amount=outcome.buy_amount).total
    sell_cost = trade_costs(profile, side="sell", gross_amount=outcome.sell_amount).total
    capital = outcome.allocated_capital or (outcome.buy_amount + buy_cost)
    if capital <= 0 or outcome.buy_amount + buy_cost > capital + 1e-8:
        return None
    return (outcome.sell_amount - sell_cost - outcome.buy_amount - buy_cost) / capital


def affordable_execution_purchase(
    notional: float, price: float, minimum: int, step: int, profile: PaperCostProfile,
    *, gross_limit: float | None = None,
) -> tuple[int, float, float]:
    """Find the largest legal lot count under settled cash and gross limits.

    Registered nonnegative costs make the settled debit monotone in quantity.
    Probe the top two lots first for ordinary fee adjustments, then bisect.
    Currency rounding and the original gross-based upper bound stay unchanged.
    """
    cash = Decimal(str(notional))
    capacity = cash if gross_limit is None else Decimal(str(gross_limit))
    unit_price = Decimal(str(price))
    lower = (minimum + step - 1) // step
    upper = math.floor(min(cash, capacity) / unit_price / step)
    best = (0, 0.0, 0.0)
    probes = 0
    while lower <= upper:
        lots = upper if probes < 2 else (lower + upper) // 2
        quantity = lots * step
        gross = round(float(unit_price * quantity), 2)
        cost = trade_costs(profile, side="buy", gross_amount=gross).total
        settled_gross = Decimal(str(gross))
        if settled_gross <= capacity and settled_gross + Decimal(str(cost)) <= cash:
            best = (quantity, gross, cost)
            lower = lots + 1
        else:
            upper = lots - 1
        probes += 1
    return best


def frozen_slot_summary(
    outcomes: Sequence[ExecutionOutcome | None],
    *,
    cost_profile: CostProfileName | None = None,
    minimum_coverage: float = 0.95,
) -> dict[str, object]:
    values = [execution_scenario_value(outcome, cost_profile) for outcome in outcomes]
    known = [value for value in values if value is not None]
    coverage = len(known) / len(outcomes) if outcomes else 0.0
    complete = bool(outcomes) and len(known) == len(outcomes)
    reasons = []
    if coverage < minimum_coverage:
        reasons.append("minimum_outcome_coverage")
    if not complete:
        reasons.append("unresolved_frozen_slots")
    return {
        "net_return": fmean(known) if complete else None,
        "status": "ok" if not reasons else "insufficient_data",
        "expected_slot_count": len(outcomes),
        "known_slot_count": len(known),
        "outcome_coverage": coverage,
        "minimum_outcome_coverage": minimum_coverage,
        "coverage_gate_passed": coverage >= minimum_coverage,
        "insufficient_reasons": reasons,
        **_slot_status_counts(outcomes, values),
        "conditional_known_slot_return": fmean(known) if known else None,
        "semantics": "equal-weight-frozen-slots; unfilled-entry=cash; unresolved-position=null",
    }


def _slot_status_counts(
    outcomes: Sequence[ExecutionOutcome | None], values: Sequence[float | None],
) -> dict[str, object]:
    return {
        "status_counts": dict(sorted(Counter(
            outcome.status if outcome is not None else "data_unavailable" for outcome in outcomes
        ).items())),
        "cash_slot_count": sum(
            outcome is not None and outcome.status == "unfilled" and value == 0
            for outcome, value in zip(outcomes, values, strict=True)
        ),
        "open_position_count": sum(outcome is not None and outcome.position_open for outcome in outcomes),
    }


def execution_cost_diagnostics(outcomes: Sequence[ExecutionOutcome | None]) -> dict[str, object]:
    modelled = [item for item in outcomes if item is not None and item.status == "modelled"]
    cost_drag = [item.cost_drag for item in modelled if item.cost_drag is not None]
    return {
        "modelled_sample_size": len(modelled),
        "average_cost_drag": fmean(cost_drag) if cost_drag else None,
        "delayed_exit_count": sum(item.exit_delay_sessions > 0 for item in modelled),
        "model_limited_count": sum(item is not None and item.model_limited for item in outcomes),
    }
