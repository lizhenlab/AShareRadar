from __future__ import annotations

import pytest

from app.services.market_scan_research_portfolio import ResearchPortfolioConfig, replay_research_portfolio
from tests.test_market_scan_research_portfolio import DATES, batch, replay, row


@pytest.mark.parametrize("lots", [1, 2, 3])
def test_commission_uses_cash_without_consuming_gross_turnover_capacity(lots: int) -> None:
    capacity = 1000 * lots
    rows = tuple(row(day, amount=capacity * 100 if day == DATES[0] else 100_000_000) for day in DATES)
    result = replay(rows=rows)
    buy = result.trades[0]
    assert buy.side == "buy" and buy.quantity == 100 * lots
    assert buy.gross_amount == capacity and buy.gross_amount + buy.fees <= 10_000
    assert buy.fees > 0
    assert result.days[-1].cash == pytest.approx(20_000 - result.total_fees)


def test_participation_product_does_not_lose_a_cent_before_sizing_the_order() -> None:
    rows = tuple(row(day, price=.29, previous=.29, amount=100 if day == DATES[0] else 100_000_000) for day in DATES)
    result = replay(rows=rows, max_participation_rate=.29)
    buy = result.trades[0]
    assert buy.quantity == 100 and buy.gross_amount == 29
    assert buy.gross_amount + buy.fees > 29


def test_gross_capacity_one_cent_below_a_lot_still_prevents_entry() -> None:
    rows = tuple(row(day, amount=99_999 if day == DATES[0] else 100_000_000) for day in DATES)
    result = replay(rows=rows)
    assert result.trades == ()
    assert result.events[0].reason == "cash_or_prior_capacity_below_minimum_lot"
    assert result.total_return == 0 and result.days[-1].cash == 20_000


def test_sufficient_gross_capacity_does_not_excuse_missing_commission_cash() -> None:
    rows = tuple(row(day, amount=100_000 if day == DATES[0] else 100_000_000) for day in DATES)
    result = replay_research_portfolio(
        (batch(DATES[0]),), DATES, synthetic_rows=rows,
        config=ResearchPortfolioConfig(initial_cash=2000, top_n=1, horizon=1),
    )
    assert result.trades == ()
    assert result.days[-1].cash == 2000 and result.total_fees == 0


def test_exact_cent_sleeve_cash_is_not_floored_twice_before_purchase() -> None:
    rows = tuple(row(day, price=20.43, previous=20.43) for day in DATES)
    result = replay_research_portfolio(
        (batch(DATES[0]),), DATES, synthetic_rows=rows,
        config=ResearchPortfolioConfig(initial_cash=4096.86, top_n=1, horizon=1),
    )
    buy = result.trades[0]
    assert buy.side == "buy" and buy.quantity == 100
    assert buy.gross_amount == 2043 and buy.fees == 5.43
    assert buy.sleeve_cash_after == 0
    assert result.days[-1].cash == pytest.approx(4096.86 - result.total_fees)


@pytest.mark.parametrize("previous_amount,exit_index", [(10_900, 2), (10_899, 3)])
def test_exit_capacity_compares_the_same_cent_amount_that_is_booked(previous_amount: int, exit_index: int) -> None:
    rows = tuple(row(day, price=1.09, previous=1.09,
                     amount=20_000 if day == DATES[0] else previous_amount if day == DATES[1] else 100_000_000)
                 for day in DATES)
    result = replay(rows=rows)
    buy, sell = result.trades
    assert buy.quantity == sell.quantity == 100
    assert sell.session_date == DATES[exit_index] and sell.gross_amount == 109
    assert sell.exit_delay_sessions == exit_index - 2
    assert result.days[-1].cash == pytest.approx(20_000 - result.total_fees)
    assert bool(result.events) is (exit_index == 3)
