"""Cash-feasible maximum lots and bounded work across real replay callers."""

from dataclasses import asdict
from decimal import Decimal
import math
from random import Random

import pytest

from app.models.paper_trading import PaperCostOverrides
from app.services import market_scan_evaluation_execution as execution
from app.services import market_scan_research_portfolio as portfolio
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs
from tests.test_market_scan_research_portfolio import DATES, batch, row


def _exhaustive_purchase(cash, price, minimum, step, profile, *, gross_limit=None):
    """Independent exhaustive feasible set; intentionally small test budgets."""
    cash, price = Decimal(str(cash)), Decimal(str(price))
    capacity = cash if gross_limit is None else Decimal(str(gross_limit))
    bound = math.floor(min(cash, capacity) / price / step)
    feasible = [(0, 0.0, 0.0)]
    for lots in range(1, bound + 1):
        quantity = lots * step
        if quantity < minimum:
            continue
        gross = round(float(price * quantity), 2)
        fees = trade_costs(profile, side="buy", gross_amount=gross).total
        if Decimal(str(gross)) <= capacity and Decimal(str(gross)) + Decimal(str(fees)) <= cash:
            feasible.append((quantity, gross, fees))
    return max(feasible)


def _profile(name):
    if name == "zero":
        return resolve_cost_profile("base", PaperCostOverrides(
            commission_rate_pct=0, minimum_commission=0, transfer_fee_pct=0, slippage_buy_pct=0,
        ))
    if name == "large-minimum":
        return resolve_cost_profile("base", PaperCostOverrides(minimum_commission=10000))
    return resolve_cost_profile(name)


@pytest.mark.parametrize("name", ["base", "conservative", "stress", "zero", "large-minimum"])
@pytest.mark.parametrize("minimum,step", [(100, 100), (200, 1), (201, 100), (1, 1)])
def test_search_matches_complete_feasible_set_at_currency_and_capacity_boundaries(name, minimum, step):
    rng = Random(4096)
    profile = _profile(name)
    for _ in range(25):
        price = rng.choice([.01, .07, 1.09, 2.18, 7.125, 99.995])
        cash = float(Decimal(str(price)) * rng.randrange(0, 450) * step + Decimal(rng.choice([0, 1, 5, 999])) / 100)
        capacity = rng.choice([None, 0.0, cash, cash / 2, max(0, cash - .001)])
        expected = _exhaustive_purchase(cash, price, minimum, step, profile, gross_limit=capacity)
        actual = execution.affordable_execution_purchase(cash, price, minimum, step, profile, gross_limit=capacity)
        assert actual == expected, (name, cash, price, minimum, step, capacity)


@pytest.mark.parametrize("cash,price,minimum,step", [(10_000_000, 1, 200, 1), (1_000_000_000_000, .01, 100, 1)])
def test_large_cash_search_is_logarithmic_and_returns_the_maximum(monkeypatch, cash, price, minimum, step):
    profile = resolve_cost_profile("stress")
    calls = []

    def counted(*args, **kwargs):
        calls.append(kwargs["gross_amount"])
        return trade_costs(*args, **kwargs)

    monkeypatch.setattr(execution, "trade_costs", counted)
    quantity, gross, fees = execution.affordable_execution_purchase(cash, price, minimum, step, profile)
    assert quantity >= minimum and quantity % step == 0
    assert Decimal(str(gross)) + Decimal(str(fees)) <= Decimal(str(cash))
    next_gross = round(float(Decimal(str(price)) * (quantity + step)), 2)
    next_fees = trade_costs(profile, side="buy", gross_amount=next_gross).total
    assert Decimal(str(next_gross)) + Decimal(str(next_fees)) > Decimal(str(cash))
    maximum_lots = math.floor(Decimal(str(cash)) / Decimal(str(price)) / step)
    assert len(calls) <= maximum_lots.bit_length() + 2


@pytest.mark.parametrize("cash,price,limit,expected_calls", [(10_000, 10, None, 2), (100_000_000, 1, 10_000, 1)])
def test_ordinary_one_lot_adjustment_and_capacity_bound_keep_fast_paths(monkeypatch, cash, price, limit, expected_calls):
    calls = []

    def counted(*args, **kwargs):
        calls.append(kwargs["gross_amount"])
        return trade_costs(*args, **kwargs)

    monkeypatch.setattr(execution, "trade_costs", counted)
    result = execution.affordable_execution_purchase(cash, price, 100, 100, resolve_cost_profile("stress"), gross_limit=limit)
    assert result[0] > 0
    assert len(calls) == expected_calls


@pytest.mark.parametrize("name", ["base", "conservative", "stress"])
@pytest.mark.parametrize("amount", [10_000., 100_000., 100_000_000.])
def test_public_shared_account_replay_keeps_the_entire_ledger_and_digest(monkeypatch, name, amount):
    batches = (batch(DATES[0]), batch(DATES[2]))
    rows = tuple(row(day, amount=amount, exit_state="locked_limit" if day == DATES[2] else "executable") for day in DATES)
    settings = portfolio.ResearchPortfolioConfig(initial_cash=20_000, top_n=1, horizon=1, cost_profile=name)
    actual = portfolio.replay_research_portfolio(batches, DATES, synthetic_rows=rows, config=settings)
    monkeypatch.setattr(portfolio, "affordable_execution_purchase", _exhaustive_purchase)
    expected = portfolio.replay_research_portfolio(batches, DATES, synthetic_rows=rows, config=settings)
    assert asdict(actual) == asdict(expected)
