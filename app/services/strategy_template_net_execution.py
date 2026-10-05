"""Frozen-weight, independent-batch execution against admitted official rows."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, ROUND_DOWN
from math import fsum, isfinite

from app.models.paper_trading import PaperCostProfile
from app.services.market_scan_evaluation_execution import affordable_execution_purchase
from app.services.market_scan_official_execution import OfficialExecutionSessionRow
from app.services.market_scan_research_portfolio_admission import research_row_status
from app.services.paper_trading_costs import trade_costs
from app.services.strategy_template_tracking_metrics import StrategyTrackingPosition, StrategyTrackingSelection


_MAX_EXACT_JSON_QUANTITY = 2 ** 53 - 1


@dataclass(frozen=True)
class FrozenNetScenarioContext:
    rows: Mapping[tuple[str, str], OfficialExecutionSessionRow]
    unavailable_dates: frozenset[str]
    days: tuple[str, ...]
    completed_date: str
    notional: float
    profile: PaperCostProfile
    max_participation: float
    evidence_available: bool


@dataclass(frozen=True)
class _PositionOutcome:
    symbol: str
    target_weight: float
    budget_cny: float
    status: str = "unavailable"
    reason: str | None = None
    quantity: int = 0
    buy_amount_cny: float = 0.0
    sell_amount_cny: float = 0.0
    buy_fees_cny: float = 0.0
    sell_fees_cny: float = 0.0
    pnl_cny: float | None = None
    close_values_cny: tuple[float, ...] = ()


def evaluate_frozen_net_scenario(selection: StrategyTrackingSelection, context: FrozenNetScenarioContext) -> dict[str, object]:
    reason = _scenario_reason(selection, context)
    positions = tuple(_position_outcome(position, context, reason) for position in selection.positions)
    unresolved = sum(item.status in {"unavailable", "pending", "blocked"} for item in positions)
    status = _scenario_status(reason, unresolved)
    pnl = _finite_sum(tuple(float(item.pnl_cny or 0) for item in positions)) if status == "available" else None
    drawdown = _batch_drawdown(positions, context) if status == "available" else None
    fees = _finite_sum(tuple(item.buy_fees_cny + item.sell_fees_cny for item in positions))
    if status == "available" and None in (pnl, drawdown, fees):
        status, reason, pnl, drawdown = "unavailable", "nonfinite_batch_valuation", None, None
    return _scenario_payload(positions, context, status, reason, (pnl, drawdown, fees))


def _scenario_payload(
    positions: tuple[_PositionOutcome, ...], context: FrozenNetScenarioContext, status: str, reason: str | None,
    measurements: tuple[float | None, float | None, float | None],
) -> dict[str, object]:
    pnl, drawdown, fees = measurements
    return {
        "status": status, "net_return": pnl / context.notional if pnl is not None else None,
        "total_fees_cny": round(fees, 2) if fees is not None else None,
        "filled_count": sum(item.quantity > 0 for item in positions),
        "unfilled_count": sum(item.status == "unfilled" for item in positions),
        "unavailable_count": sum(item.status in {"unavailable", "pending", "blocked"} for item in positions),
        "residual_cash_cny": round(context.notional + pnl, 2) if pnl is not None else None,
        "independent_batch_max_drawdown": drawdown,
        "reason_counts": dict(sorted(Counter([reason] if reason else [item.reason for item in positions if item.reason]).items())),
        "positions": [_position_payload(item) for item in positions],
    }


def _position_payload(item: _PositionOutcome) -> dict[str, object]:
    return {key: value for key, value in asdict(item).items() if key != "close_values_cny"}


def _scenario_reason(selection: StrategyTrackingSelection, context: FrozenNetScenarioContext) -> str | None:
    if selection.status == "blocked":
        return "selection_blocked"
    if not context.days:
        return "calendar_unavailable"
    if context.days[-1] > context.completed_date:
        return "outcome_not_mature"
    if not selection.positions:
        return "no_selection" if selection.status == "no_trade" else "empty_ready_selection"
    if not context.evidence_available:
        return "official_execution_evidence_unavailable"
    if context.days[1] < context.profile.effective_from:
        return "cost_profile_not_effective"
    return None


def _scenario_status(reason: str | None, unresolved: int) -> str:
    if reason == "selection_blocked":
        return "blocked"
    if reason == "outcome_not_mature":
        return "pending"
    if unresolved or reason not in {None, "no_selection"}:
        return "unavailable"
    return "available"


def _position_outcome(position: StrategyTrackingPosition, context: FrozenNetScenarioContext, reason: str | None) -> _PositionOutcome:
    budget = float((Decimal(str(context.notional)) * Decimal(str(position.target_weight))).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
    outcome = _PositionOutcome(position.symbol, position.target_weight, budget)
    if reason:
        return replace(outcome, status=_scenario_status(reason, 1), reason=reason)
    current = context.rows.get((position.symbol, context.days[1]))
    previous = context.rows.get((position.symbol, context.days[0]))
    status, entry_reason = _entry_admission(current, previous, context)
    if entry_reason:
        return _unfilled(outcome, context, entry_reason) if status == "unfilled" else replace(outcome, reason=entry_reason)
    assert current is not None and previous is not None and current.bar.open is not None
    rules = current.instrument_rules
    quantity, amount, fees = affordable_execution_purchase(
        budget, current.bar.open, rules.minimum_buy_quantity, rules.buy_quantity_step, context.profile,
        gross_limit=float(Decimal(str(previous.bar.amount)) * Decimal(str(context.max_participation))),
    )
    if not quantity:
        return _unfilled(outcome, context, "cash_or_prior_capacity_below_minimum_lot")
    if quantity > _MAX_EXACT_JSON_QUANTITY:
        return replace(outcome, reason="position_quantity_not_representable")
    entered = replace(outcome, quantity=quantity, buy_amount_cny=round(amount, 2), buy_fees_cny=fees)
    return _exit_outcome(entered, context)


def _entry_admission(
    current: OfficialExecutionSessionRow | None, previous: OfficialExecutionSessionRow | None, context: FrozenNetScenarioContext,
) -> tuple[str, str | None]:
    if context.days[1] in context.unavailable_dates:
        return "unavailable", "execution_evidence_after_as_of"
    if current is None:
        return "unavailable", "entry_session_evidence_missing"
    if current.corporate_action.status != "none":
        return "unavailable", "corporate_action_ledger_required"
    if current.exchange_session_state != "trading":
        return "unfilled", "entry_session_" + current.exchange_session_state
    if current.entry_execution_state != "executable":
        return "unfilled", "entry_" + current.entry_execution_state
    if context.days[0] in context.unavailable_dates:
        return "unavailable", "execution_evidence_after_as_of"
    return "unavailable", research_row_status(current, previous)


def _unfilled(outcome: _PositionOutcome, context: FrozenNetScenarioContext, reason: str) -> _PositionOutcome:
    return replace(outcome, status="unfilled", reason=reason, pnl_cny=0.0,
                   close_values_cny=(outcome.budget_cny,) * (len(context.days) - 1))


def _path_reason(symbol: str, context: FrozenNetScenarioContext) -> str | None:
    previous = None
    for index, day in enumerate(context.days):
        current = context.rows.get((symbol, day))
        if day in context.unavailable_dates:
            return "execution_evidence_after_as_of"
        if current is None:
            return "holding_path_evidence_missing"
        if current.corporate_action.status != "none":
            return "corporate_action_ledger_required"
        if index and (reason := research_row_status(current, previous)):
            return reason
        previous = current
    return None


def _exit_outcome(outcome: _PositionOutcome, context: FrozenNetScenarioContext) -> _PositionOutcome:
    reason = _path_reason(outcome.symbol, context)
    if reason:
        return replace(outcome, reason=reason)
    current = context.rows[(outcome.symbol, context.days[-1])]
    previous = context.rows[(outcome.symbol, context.days[-2])]
    assert current.bar.close is not None
    gross = _share_value(outcome.quantity, current.bar.close)
    reason = _exit_reason(outcome, current, previous, gross, context)
    if reason:
        return replace(outcome, reason=reason)
    fees = trade_costs(context.profile, side="sell", gross_amount=gross).total
    pnl = round(gross - fees - outcome.buy_amount_cny - outcome.buy_fees_cny, 2)
    marks = _position_marks(outcome, context, pnl)
    if marks is None:
        return replace(outcome, reason="nonfinite_position_valuation")
    return replace(outcome, status="available", sell_amount_cny=gross, sell_fees_cny=fees, pnl_cny=pnl, close_values_cny=marks)


def _exit_reason(
    outcome: _PositionOutcome, current: OfficialExecutionSessionRow, previous: OfficialExecutionSessionRow,
    gross: float, context: FrozenNetScenarioContext,
) -> str | None:
    if not isfinite(gross):
        return "nonfinite_exit_amount"
    if current.exit_execution_state != "executable":
        return "exit_" + current.exit_execution_state
    if outcome.quantity % current.instrument_rules.sell_quantity_step:
        return "exit_quantity_rule_conflict"
    if Decimal(str(gross)) > Decimal(str(previous.bar.amount)) * Decimal(str(context.max_participation)):
        return "exit_prior_session_capacity_exceeded"
    fees = trade_costs(context.profile, side="sell", gross_amount=gross).total
    if outcome.budget_cny + gross - outcome.buy_amount_cny - outcome.buy_fees_cny - fees < 0:
        return "exit_fee_cash_shortfall"
    return None


def _position_marks(outcome: _PositionOutcome, context: FrozenNetScenarioContext, pnl: float) -> tuple[float, ...] | None:
    cash = outcome.budget_cny - outcome.buy_amount_cny - outcome.buy_fees_cny
    marks = [round(cash + _share_value(outcome.quantity, float(context.rows[(outcome.symbol, day)].bar.close or 0)), 2)
             for day in context.days[1:-1]]
    marks.append(round(outcome.budget_cny + pnl, 2))
    return tuple(marks) if all(isfinite(value) and value >= 0 for value in marks) else None


def _batch_drawdown(positions: tuple[_PositionOutcome, ...], context: FrozenNetScenarioContext) -> float | None:
    cash = context.notional - fsum(item.budget_cny for item in positions)
    peak, drawdown = context.notional, 0.0
    for index in range(max(0, len(context.days) - 1)):
        value = _finite_sum((cash, *(item.close_values_cny[index] for item in positions)))
        if value is None:
            return None
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    return drawdown


def _finite_sum(values: tuple[float, ...]) -> float | None:
    try:
        value = fsum(values)
    except OverflowError:
        return None
    return value if isfinite(value) else None


def _share_value(quantity: int, price: float) -> float:
    """Multiply exactly before converting, without coercing an integer to float."""
    return round(float(Decimal(quantity) * Decimal(str(price))), 2)
