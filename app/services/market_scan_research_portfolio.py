"""Replay one shared cash account across overlapping frozen daily signals.

No prices, execution evidence, ranks or corporate actions are fetched or repaired.
Official provenance requires existing strict-loader tokens. Valid ordinary rows
are useful deterministic fixtures, but always remain explicitly synthetic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal
import json
from typing import cast

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.paper_trading import PaperCostProfile
from app.services.market_scan_evaluation_execution import affordable_execution_purchase
from app.services.market_scan_official_execution import OfficialExecutionSessionRow, VerifiedOfficialExecutionSession
from app.services.market_scan_research_portfolio_admission import (
    admit_research_market_data, research_input_digest, research_row_status, validate_research_inputs,
)
from app.services.market_scan_research_portfolio_models import (
    RESEARCH_CAPITAL_POLICY, RESEARCH_PORTFOLIO_VERSION, ResearchMarketData,
    ResearchPortfolioConfig, ResearchPortfolioDay, ResearchPortfolioEvent, ResearchPortfolioPosition,
    ResearchPortfolioResult, ResearchPortfolioTrade, ResearchReplayState, ResearchSignalBatch, ResearchSignalCandidate,
)
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs
from app.services.trading_calendar import next_trade_dates


@dataclass(frozen=True)
class _ReplayContext:
    config: ResearchPortfolioConfig
    market: ResearchMarketData
    sessions: tuple[str, ...]
    cost: PaperCostProfile


_UNRESOLVED_SHARE_LEDGER = {"corporate_action_ledger_required", "corporate_action_history_incomplete"}
_UNKNOWN_ENTRY_REASONS = {
    "session_evidence_missing", "previous_session_missing", "previous_close_reference_conflict",
    "corporate_action_ledger_required", "session_valuation_missing",
}


def replay_research_portfolio(
    batches: Sequence[ResearchSignalBatch], sessions: Sequence[str], *,
    official_sessions: Sequence[VerifiedOfficialExecutionSession] = (),
    synthetic_rows: Sequence[OfficialExecutionSessionRow] = (),
    config: ResearchPortfolioConfig | None = None,
) -> ResearchPortfolioResult:
    """Return a canonical ledger; no result alone authorizes model promotion."""
    settings = config or ResearchPortfolioConfig()
    validate_research_inputs(batches, sessions)
    if settings.allocation == "frozen-universe" and any(not batch.candidates for batch in batches):
        raise ValueError("frozen-universe allocation requires a nonempty universe in every batch")
    market = admit_research_market_data(official_sessions, synthetic_rows)
    context = _ReplayContext(settings, market, tuple(sessions), resolve_cost_profile(settings.cost_profile))
    if sessions[0] < context.cost.effective_from:
        raise ValueError("research window predates the selected cost profile")
    state = ResearchReplayState(_initial_sleeve_cash(settings), nav_peak=settings.initial_cash)
    by_signal = {batch.signal_date: batch for batch in batches}
    for index, _session in enumerate(sessions):
        offset = len(state.trades)
        if index and (batch := by_signal.get(sessions[index - 1])) is not None:
            _enter_batch(state, context, batch, index)
        _exit_matured_positions(state, context, index)
        _record_day(state, context, index, offset)
    if sessions[-1] in by_signal:
        batch = by_signal[sessions[-1]]
        state.unknown_entry_slots += _slot_count(settings, batch)
        state.events.append(ResearchPortfolioEvent(sessions[-1], batch.batch_id, -1, None, "entry_session_after_window"))
    digest = research_input_digest(batches, sessions, market, settings)
    return _result(state, context, digest, batches)


def _initial_sleeve_cash(config: ResearchPortfolioConfig) -> list[float]:
    sleeves = config.horizon + 1
    cents, remainder = divmod(round(config.initial_cash * 100), sleeves)
    return [(cents + int(index < remainder)) / 100 for index in range(sleeves)]


def _slot_count(config: ResearchPortfolioConfig, batch: ResearchSignalBatch) -> int:
    return len(batch.candidates) if config.allocation == "frozen-universe" else config.top_n


def _enter_batch(state: ResearchReplayState, context: _ReplayContext, batch: ResearchSignalBatch, index: int) -> None:
    sleeve = (index - 1) % (context.config.horizon + 1)
    day = context.sessions[index]
    if state.unknown_entry_slots:
        state.unknown_entry_slots += _slot_count(context.config, batch)
        state.events.append(ResearchPortfolioEvent(day, batch.batch_id, sleeve, None, "account_entry_history_incomplete"))
        return
    if any(position.sleeve == sleeve for position in state.positions):
        state.events.append(ResearchPortfolioEvent(day, batch.batch_id, sleeve, None, "sleeve_still_invested"))
        return
    slots = _slot_count(context.config, batch)
    budget = (int(round(state.cash[sleeve] * 100)) // slots) / 100
    selected = sorted((item for item in batch.candidates if context.config.allocation == "frozen-universe" or item.frozen_rank <= slots), key=lambda item: item.frozen_rank)
    if len(selected) < slots:
        state.unknown_entry_slots += slots - len(selected)
        state.events.append(ResearchPortfolioEvent(day, batch.batch_id, sleeve, None, "missing_frozen_slots_retain_cash"))
    for candidate in selected:
        _enter_candidate(state, context, batch, candidate, index, sleeve, budget)


def _enter_candidate(
    state: ResearchReplayState, context: _ReplayContext, batch: ResearchSignalBatch,
    candidate: ResearchSignalCandidate, index: int, sleeve: int, budget: float,
) -> None:
    day = context.sessions[index]
    current = context.market.rows.get((candidate.symbol, day))
    previous = context.market.rows.get((candidate.symbol, context.sessions[index - 1]))
    reason = _entry_reason(state, current, previous, candidate.symbol)
    if reason is not None:
        state.unknown_entry_slots += int(reason in _UNKNOWN_ENTRY_REASONS)
        state.events.append(ResearchPortfolioEvent(day, batch.batch_id, sleeve, candidate.symbol, reason))
        return
    assert current is not None and previous is not None and current.bar.open is not None
    capacity = float(Decimal(str(previous.bar.amount or 0)) * Decimal(str(context.config.max_participation_rate)))
    rules = current.instrument_rules
    quantity, gross, fees = affordable_execution_purchase(
        budget, current.bar.open, rules.minimum_buy_quantity, rules.buy_quantity_step, context.cost, gross_limit=capacity,
    )
    if not quantity:
        state.events.append(ResearchPortfolioEvent(day, batch.batch_id, sleeve, candidate.symbol, "cash_or_prior_capacity_below_minimum_lot"))
        return
    gross = round(gross, 2)
    state.cash[sleeve] = round(state.cash[sleeve] - gross - fees, 2)
    state.cumulative_fees = round(state.cumulative_fees + fees, 2)
    target = next_trade_dates(date.fromisoformat(batch.signal_date), context.config.horizon + 1)[-1].isoformat()
    position = ResearchPortfolioPosition(
        batch.batch_id, batch.source_digest, candidate.source_identity, candidate.symbol, candidate.frozen_rank,
        sleeve, quantity, day, target, current.bar.open, fees, round(gross + fees, 2),
    )
    state.positions.append(position)
    state.trades.append(_trade(position, current, "buy", gross, fees, state.cash[sleeve]))


def _entry_reason(
    state: ResearchReplayState, current: OfficialExecutionSessionRow | None,
    previous: OfficialExecutionSessionRow | None, symbol: str,
) -> str | None:
    if any(position.symbol == symbol for position in state.positions):
        return "symbol_already_held"
    # Admitted session states can prove no purchase without reconstructing a
    # price. This does not excuse missing valuation evidence for held positions.
    if current is not None and current.exchange_session_state != "trading":
        return "session_" + current.exchange_session_state
    if current is not None and current.entry_execution_state != "executable":
        return "entry_" + current.entry_execution_state
    return research_row_status(current, previous)


def _exit_matured_positions(state: ResearchReplayState, context: _ReplayContext, index: int) -> None:
    remaining = []
    for position in state.positions:
        current = context.market.rows.get((position.symbol, context.sessions[index]))
        if current is None:
            position = replace(position, unresolved_reason="corporate_action_history_incomplete")
        elif current.corporate_action.status != "none":
            position = replace(position, unresolved_reason="corporate_action_ledger_required")
        if context.sessions[index] < position.target_exit_date:
            remaining.append(position)
            continue
        reason = _exit_reason(position, context, index, state.cash[position.sleeve])
        if reason is not None:
            state.events.append(ResearchPortfolioEvent(context.sessions[index], position.batch_id, position.sleeve, position.symbol, reason))
            remaining.append(position)
            continue
        assert current is not None and current.bar.close is not None
        gross = round(position.quantity * current.bar.close, 2)
        fees = trade_costs(context.cost, side="sell", gross_amount=gross).total
        state.cash[position.sleeve] = round(state.cash[position.sleeve] + gross - fees, 2)
        state.cumulative_fees = round(state.cumulative_fees + fees, 2)
        delay = sum(position.target_exit_date < day <= current.session_date for day in context.sessions)
        state.trades.append(_trade(position, current, "sell", gross, fees, state.cash[position.sleeve], delay))
    state.positions = remaining


def _exit_reason(position: ResearchPortfolioPosition, context: _ReplayContext, index: int, sleeve_cash: float) -> str | None:
    if position.unresolved_reason in _UNRESOLVED_SHARE_LEDGER:
        return position.unresolved_reason
    current = context.market.rows.get((position.symbol, context.sessions[index]))
    previous = context.market.rows.get((position.symbol, context.sessions[index - 1])) if index else None
    reason = research_row_status(current, previous)
    if reason is not None:
        return reason
    assert current is not None and previous is not None and current.bar.close is not None
    if current.exit_execution_state != "executable":
        return "exit_" + current.exit_execution_state
    if position.quantity % current.instrument_rules.sell_quantity_step:
        return "exit_quantity_rule_conflict"
    gross = round(position.quantity * current.bar.close, 2)
    capacity = Decimal(str(previous.bar.amount or 0)) * Decimal(str(context.config.max_participation_rate))
    if Decimal(str(gross)) > capacity:
        return "exit_prior_session_capacity_exceeded"
    if round(sleeve_cash + gross - trade_costs(context.cost, side="sell", gross_amount=gross).total, 2) < 0:
        return "exit_fee_cash_shortfall"
    return None


def _trade(
    position: ResearchPortfolioPosition, row: OfficialExecutionSessionRow, side: str,
    gross: float, fees: float, cash: float, delay: int = 0,
) -> ResearchPortfolioTrade:
    price = row.bar.open if side == "buy" else row.bar.close
    assert price is not None
    return ResearchPortfolioTrade(
        row.session_date, "buy" if side == "buy" else "sell", position.batch_id, position.source_digest,
        position.source_identity, position.symbol, position.frozen_rank, position.sleeve, position.quantity,
        price, gross, fees, cash, row.row_digest, delay,
    )


def _mark_position(position: ResearchPortfolioPosition, context: _ReplayContext, index: int) -> ResearchPortfolioPosition:
    current = context.market.rows.get((position.symbol, context.sessions[index]))
    previous = context.market.rows.get((position.symbol, context.sessions[index - 1])) if index else None
    reason = position.unresolved_reason if position.unresolved_reason in _UNRESOLVED_SHARE_LEDGER else research_row_status(current, previous)
    if reason is not None:
        return replace(position, mark_price=None, market_value=None, unresolved_reason=reason)
    assert current is not None and current.bar.close is not None
    return replace(position, mark_price=current.bar.close, market_value=round(position.quantity * current.bar.close, 2), unresolved_reason=None)


def _valuation_marks(
    state: ResearchReplayState, context: _ReplayContext, index: int,
) -> tuple[tuple[ResearchPortfolioPosition, ...], tuple[str, ...], float | None]:
    positions = tuple(sorted((_mark_position(item, context, index) for item in state.positions), key=lambda item: (item.sleeve, item.frozen_rank, item.symbol)))
    reasons = tuple(sorted({item.unresolved_reason for item in positions if item.unresolved_reason is not None}))
    if state.unknown_entry_slots:
        reasons += ("entry_decision_evidence_incomplete",)
    market_value = None if reasons else round(sum(item.market_value or 0 for item in positions), 2)
    return positions, reasons, market_value


def _record_day(state: ResearchReplayState, context: _ReplayContext, index: int, trade_offset: int) -> None:
    positions, reasons, market_value = _valuation_marks(state, context, index)
    cash = round(sum(state.cash), 2)
    nav = None if market_value is None else round(cash + market_value, 2)
    previous_nav = state.days[-1].nav if state.days else context.config.initial_cash
    daily_return = nav / previous_nav - 1 if nav is not None and previous_nav is not None and previous_nav > 0 else None
    if nav is None:
        state.valuation_gap = True
    else:
        state.nav_peak = max(state.nav_peak, nav)
    drawdown = nav / state.nav_peak - 1 if nav is not None and not state.valuation_gap else None
    trades = state.trades[trade_offset:]
    gross_traded = round(sum(item.gross_amount for item in trades), 2)
    day = ResearchPortfolioDay(
        context.sessions[index], cash, tuple(state.cash), market_value, nav, daily_return, drawdown,
        gross_traded, gross_traded / previous_nav if previous_nav is not None and previous_nav > 0 else None,
        round(sum(item.cost_basis for item in positions), 2), round(sum(item.fees for item in trades), 2),
        state.cumulative_fees, positions, reasons,
    )
    state.days.append(day)


def _result(
    state: ResearchReplayState, context: _ReplayContext, digest: str, batches: Sequence[ResearchSignalBatch],
) -> ResearchPortfolioResult:
    last = state.days[-1]
    filled = sum(item.side == "buy" for item in state.trades)
    expected = sum(_slot_count(context.config, batch) for batch in batches)
    complete = not state.valuation_gap and not state.unknown_entry_slots
    drawdowns = [day.drawdown for day in state.days if day.drawdown is not None]
    result = ResearchPortfolioResult(
        RESEARCH_PORTFOLIO_VERSION, RESEARCH_CAPITAL_POLICY, context.config, context.market.provenance_status,
        context.market.source_artifact_digests, digest, "", tuple(state.days), tuple(state.trades), tuple(state.events),
        last.positions, state.cumulative_fees,
        last.nav / context.config.initial_cash - 1 if complete and last.nav is not None else None,
        min(drawdowns) if complete and drawdowns else None,
        sum(day.nav is not None for day in state.days) / len(state.days), expected, filled, filled / expected,
        state.unknown_entry_slots, (expected - state.unknown_entry_slots) / expected,
    )
    return replace(result, result_digest=sha256_hex(canonical_json_bytes(research_portfolio_payload(result))))


def research_portfolio_payload(result: ResearchPortfolioResult) -> dict[str, object]:
    """Project tuple-based immutable records to finite canonical-JSON values."""
    return cast(dict[str, object], json.loads(json.dumps(asdict(result), allow_nan=False)))


__all__ = [
    "ResearchPortfolioConfig", "ResearchSignalBatch", "ResearchSignalCandidate", "ResearchPortfolioResult",
    "replay_research_portfolio", "research_portfolio_payload",
]
