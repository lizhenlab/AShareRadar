"""Strict JSON and accounting checks for research holdings inputs."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import math
import re

from pydantic import TypeAdapter

from app.artifacts.io import sha256_hex
from app.models.paper_trading import PaperCostProfile
from app.services.market_scan_research_holdings_contracts import finite_json_bytes, require_date, require_digest
from app.services.market_scan_research_portfolio_models import (
    RESEARCH_CAPITAL_POLICY, RESEARCH_PORTFOLIO_VERSION, ResearchPortfolioConfig, ResearchPortfolioDay,
    ResearchPortfolioPosition, ResearchPortfolioResult, ResearchPortfolioTrade,
)
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs
from app.services.trading_calendar import next_trade_dates, trading_dates_between


_PORTFOLIO_ADAPTER = TypeAdapter(ResearchPortfolioResult)


def admit_holdings_portfolio(payload: object, *, expected_digest: str) -> ResearchPortfolioResult:
    encoded = finite_json_bytes(payload)
    result = _PORTFOLIO_ADAPTER.validate_json(encoded, strict=True)
    require_matching_shape(payload, asdict(result))
    if not isinstance(payload, dict):
        raise ValueError("portfolio must be a JSON object")
    unsigned = {**payload, "result_digest": ""}
    actual = sha256_hex(finite_json_bytes(unsigned))
    if result.result_digest != require_digest(expected_digest) or actual != result.result_digest:
        raise ValueError("portfolio result digest mismatch")
    validate_holdings_account(result)
    return result


def require_matching_shape(actual: object, expected: object) -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or actual.keys() != expected.keys():
            raise ValueError("portfolio JSON contains missing or unknown fields")
        for key in expected:
            require_matching_shape(actual[key], expected[key])
    elif isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)) or len(actual) != len(expected):
            raise ValueError("portfolio JSON array shape mismatch")
        for left, right in zip(actual, expected, strict=True):
            require_matching_shape(left, right)


def money(value: float, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{context} must be finite nonnegative money")
    if round(value, 2) != value:
        raise ValueError(f"{context} must be an exact cent amount")
    return value


def require_equal_money(actual: float, expected: float, context: str) -> None:
    if round(money(actual, context) * 100) != round(money(expected, context) * 100):
        raise ValueError(f"{context} violates account conservation")


def validate_holdings_account(result: ResearchPortfolioResult) -> None:
    if result.schema_version != RESEARCH_PORTFOLIO_VERSION or result.capital_policy != RESEARCH_CAPITAL_POLICY:
        raise ValueError("unsupported portfolio contract")
    if result.promotion_eligible or result.provenance_status not in {"synthetic", "unavailable", "official_raw_file_verified"}:
        raise ValueError("unsupported portfolio authority claim")
    require_digest(result.input_digest)
    for digest in result.source_artifact_digests:
        require_digest(digest)
    _validate_calendar(result)
    _validate_exit_policy(result)
    trades = _trades_by_day(result)
    if len(result.days[0].sleeve_cash) != result.config.horizon + 1:
        raise ValueError("initial sleeve count differs from fixed capital policy")
    inventory: dict[str, ResearchPortfolioTrade] = {}
    cash, fees = _initial_cash_sleeves(result.config), 0.0
    for day in result.days:
        daily_trades = trades.get(day.session_date, [])
        fees = _advance_inventory(inventory, daily_trades, cash, fees)
        require_equal_money(day.cash, round(sum(cash), 2), "daily cash")
        require_equal_money(day.cumulative_fees, fees, "cumulative fees")
        _validate_daily_cash(day, daily_trades, cash)
        _validate_day(day, inventory)
    if result.final_positions != result.days[-1].positions:
        raise ValueError("final positions mismatch")
    require_equal_money(result.total_fees, fees, "total fees")


def _initial_cash_sleeves(config: ResearchPortfolioConfig) -> list[float]:
    count = config.horizon + 1
    cents, remainder = divmod(round(config.initial_cash * 100), count)
    return [(cents + int(index < remainder)) / 100 for index in range(count)]


def _validate_daily_cash(day: ResearchPortfolioDay, trades: list[ResearchPortfolioTrade], cash: list[float]) -> None:
    if len(day.sleeve_cash) != len(cash):
        raise ValueError("daily sleeve count differs from fixed capital policy")
    for actual, expected in zip(day.sleeve_cash, cash, strict=True):
        require_equal_money(actual, expected, "daily sleeve cash")
    require_equal_money(day.fees, round(sum(trade.fees for trade in trades), 2), "daily fees")
    require_equal_money(day.gross_traded, round(sum(trade.gross_amount for trade in trades), 2), "daily gross traded")


def _validate_calendar(result: ResearchPortfolioResult) -> None:
    days = [require_date(day.session_date) for day in result.days]
    if not days or days != sorted(set(days)):
        raise ValueError("portfolio days must be unique and ordered")
    expected = [day.isoformat() for day in trading_dates_between(date.fromisoformat(days[0]), date.fromisoformat(days[-1]))]
    if days != expected:
        raise ValueError("portfolio days must follow the complete trading calendar")


def _validate_exit_policy(result: ResearchPortfolioResult) -> None:
    sessions = tuple(day.session_date for day in result.days)
    targets = {trade.session_date: _target_exit(trade.session_date, result.config.horizon, sessions)
               for trade in result.trades if trade.side == "buy"}
    purchases: dict[str, ResearchPortfolioTrade] = {}
    for trade in result.trades:
        if trade.side == "buy":
            purchases[trade.symbol] = trade
        elif (entry := purchases.pop(trade.symbol, None)) is not None:
            target = targets[entry.session_date]
            if trade.session_date < target:
                raise ValueError("sell precedes frozen target exit")
            if trade.exit_delay_sessions != sum(target < day <= trade.session_date for day in sessions):
                raise ValueError("exit delay differs from frozen account sessions")
    for day in result.days:
        for position in day.positions:
            if position.target_exit_date != targets.get(position.entry_date):
                raise ValueError("position target exit differs from frozen holding horizon")


def _target_exit(entry: str, horizon: int, sessions: tuple[str, ...]) -> str:
    offset = sessions.index(entry) + horizon
    if offset < len(sessions):
        return sessions[offset]
    projected = tuple(day.isoformat() for day in next_trade_dates(date.fromisoformat(entry), horizon))
    preserved = sessions[sessions.index(entry) + 1:]
    if projected[:len(preserved)] != preserved:
        raise ValueError("target exit calendar conflicts with retained account sessions")
    return projected[-1]


def _trades_by_day(result: ResearchPortfolioResult) -> dict[str, list[ResearchPortfolioTrade]]:
    grouped: dict[str, list[ResearchPortfolioTrade]] = {}
    dates = {day.session_date for day in result.days}
    previous = ""
    cost = resolve_cost_profile(result.config.cost_profile)
    if result.days[0].session_date < cost.effective_from:
        raise ValueError("portfolio predates its declared cost profile")
    for trade in result.trades:
        if trade.session_date not in dates or trade.session_date < previous:
            raise ValueError("trade date outside ordered account calendar")
        previous = trade.session_date
        _validate_trade(trade, result.config.horizon + 1, cost)
        grouped.setdefault(trade.session_date, []).append(trade)
    return grouped


def _validate_trade(trade: ResearchPortfolioTrade, sleeves: int, cost: PaperCostProfile) -> None:
    if trade.quantity <= 0 or not 0 <= trade.sleeve < sleeves or trade.frozen_rank <= 0:
        raise ValueError("invalid trade quantity, sleeve or rank")
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", trade.symbol) is None or not trade.batch_id:
        raise ValueError("invalid trade identity")
    for digest in (trade.source_digest, trade.source_identity, trade.execution_row_digest):
        require_digest(digest)
    if not math.isfinite(trade.price) or trade.price <= 0 or trade.exit_delay_sessions < 0:
        raise ValueError("invalid trade price or exit delay")
    require_equal_money(trade.gross_amount, round(trade.quantity * trade.price, 2), "trade gross")
    expected_fees = trade_costs(cost, side=trade.side, gross_amount=trade.gross_amount).total
    require_equal_money(trade.fees, expected_fees, "trade fees for cost profile")
    money(trade.sleeve_cash_after, "trade sleeve cash")


def _advance_inventory(
    inventory: dict[str, ResearchPortfolioTrade], trades: list[ResearchPortfolioTrade], cash: list[float], fees: float,
) -> float:
    for trade in trades:
        if trade.side == "buy":
            if trade.symbol in inventory:
                raise ValueError("duplicate held symbol")
            inventory[trade.symbol] = trade
            cash[trade.sleeve] = round(cash[trade.sleeve] - trade.gross_amount - trade.fees, 2)
        else:
            entry = inventory.pop(trade.symbol, None)
            if entry is None or (entry.batch_id, entry.sleeve, entry.quantity) != (trade.batch_id, trade.sleeve, trade.quantity):
                raise ValueError("sell does not match held inventory")
            if trade.session_date <= entry.session_date:
                raise ValueError("same-session sell violates account T+1")
            cash[trade.sleeve] = round(cash[trade.sleeve] + trade.gross_amount - trade.fees, 2)
        money(cash[trade.sleeve], "running sleeve cash")
        require_equal_money(trade.sleeve_cash_after, cash[trade.sleeve], "trade sleeve cash")
        fees = round(fees + trade.fees, 2)
    return fees


def _validate_day(day: ResearchPortfolioDay, inventory: dict[str, ResearchPortfolioTrade]) -> None:
    symbols = [position.symbol for position in day.positions]
    if len(set(symbols)) != len(symbols) or set(symbols) != inventory.keys():
        raise ValueError("daily positions do not match actual filled trades")
    require_equal_money(day.cash, round(sum(money(value, "sleeve cash") for value in day.sleeve_cash), 2), "sleeve cash sum")
    for position in day.positions:
        _validate_position(position, inventory[position.symbol], day.session_date)
    require_equal_money(day.invested_cost_basis, round(sum(item.cost_basis for item in day.positions), 2), "invested cost basis")
    _validate_nav(day)


def _validate_nav(day: ResearchPortfolioDay) -> None:
    complete = all(item.market_value is not None for item in day.positions) and not day.unresolved_reasons
    if not complete:
        if day.market_value is not None or day.nav is not None:
            raise ValueError("incomplete valuation cannot claim complete NAV")
        return
    if day.market_value is None or day.nav is None:
        raise ValueError("complete holdings valuation requires NAV")
    require_equal_money(day.market_value, round(sum(item.market_value or 0 for item in day.positions), 2), "daily market value")
    require_equal_money(day.nav, round(day.cash + day.market_value, 2), "daily NAV")


def _validate_position(position: ResearchPortfolioPosition, entry: ResearchPortfolioTrade, day: str) -> None:
    left = (position.batch_id, position.sleeve, position.quantity, position.frozen_rank, position.source_digest, position.source_identity)
    right = (entry.batch_id, entry.sleeve, entry.quantity, entry.frozen_rank, entry.source_digest, entry.source_identity)
    if left != right or position.entry_date != entry.session_date or position.entry_price != entry.price:
        raise ValueError("position does not match actual purchase")
    if require_date(position.target_exit_date) <= require_date(position.entry_date) or position.entry_date > day:
        raise ValueError("position dates violate account holding policy")
    require_equal_money(position.entry_fees, entry.fees, "position entry fees")
    require_equal_money(position.cost_basis, round(entry.gross_amount + entry.fees, 2), "position cost basis")
    if position.market_value is None or position.mark_price is None:
        if position.market_value is not None or position.mark_price is not None or not position.unresolved_reason:
            raise ValueError("missing valuation requires paired nulls and a reason")
        return
    if not math.isfinite(position.mark_price) or position.mark_price <= 0 or position.unresolved_reason is not None:
        raise ValueError("invalid position mark")
    require_equal_money(position.market_value, round(position.quantity * position.mark_price, 2), "position market value")
