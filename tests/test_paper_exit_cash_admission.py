"""The allowed commission stress model cannot create borrowed paper cash."""

import asyncio
from datetime import datetime

import pytest

from app.models.paper_trading import PaperCostOverrides, PaperSimulationRequest, PaperTradingAccountUpdate
from app.services.cache import SQLiteCache
from app.services.paper_trading import run_paper_simulation, simulate_paper_portfolio
from app.services.paper_trading_costs import resolve_cost_profile
from tests.test_paper_trading import _account, _bar, _persist_review_plan_projection, _review_plan, _strategy, _strategy_create


INITIAL_CASH = 20_002.10
STRESS_COST = resolve_cost_profile("base", PaperCostOverrides(minimum_commission=10_000))


def _account_with_cash(cash):
    request = PaperTradingAccountUpdate(initial_cash=cash)
    return _account().model_copy(update={"initial_cash": request.initial_cash})


def _rows(exit_kind="horizon_close", *, retry=False):
    entry_high = 111 if exit_kind == "t1_deferred_target" else 101
    exit_prices = {
        "horizon_close": (99, 100, 98, 99),
        "opening_stop": (89, 90, 88, 89),
        "intraday_stop": (99, 100, 89, 98),
        "t1_deferred_target": (99, 100, 98, 99),
    }[exit_kind]
    rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, entry_high, 99, 100),
        _bar("2026-07-03", *exit_prices),
    ]
    if retry:
        rows.append(_bar("2026-07-06", 101, 102, 100, 101))
    return rows


def _simulate(*, exit_kind="horizon_close", cash=INITIAL_CASH, retry=False, cost=STRESS_COST):
    strategy = _strategy(horizon_days=1 if exit_kind == "horizon_close" else 20, allocation_pct=100)
    return simulate_paper_portfolio(
        _account_with_cash(cash), [strategy], {strategy.symbol: _rows(exit_kind, retry=retry)},
        as_of=datetime(2026, 7, 6 if retry else 3, 16), cost_profile=cost,
    )


@pytest.mark.parametrize("exit_kind,pending_reason,pending_date", [
    ("horizon_close", "horizon_close", "2026-07-03"),
    ("opening_stop", "stop_hit", "2026-07-03"),
    ("intraday_stop", "stop_hit", "2026-07-03"),
    ("t1_deferred_target", "t1_deferred_target", "2026-07-02"),
])
def test_every_exit_path_retains_shares_when_fees_cannot_be_funded(exit_kind, pending_reason, pending_date):
    draft = _simulate(exit_kind=exit_kind)
    assert [item.side for item in draft.trades] == ["buy"]
    strategy = draft.strategies[0]
    assert strategy.status == "open" and strategy.quantity == 100
    assert strategy.pending_exit_reason == pending_reason and strategy.pending_exit_date == pending_date
    assert strategy.sell_friction == 0 and strategy.realized_pnl is None
    assert all(point.cash_balance >= 0 for point in draft.equity_curve)
    event = next(item for item in draft.events if item.event_code == "exit_fee_cash_shortfall")
    assert event.details["available_cash"] == 0
    assert event.details["cash_shortfall"] > 0
    assert event.details["gross_amount"] < event.details["total_cost"]


@pytest.mark.parametrize("cash,expected_sides", [(20_109.13, ["buy", "sell"]), (20_109.12, ["buy"])])
def test_exit_cash_boundary_admits_exact_coverage_and_rejects_one_cent_shortfall(cash, expected_sides):
    draft = _simulate(cash=cash)
    assert [item.side for item in draft.trades] == expected_sides
    assert draft.equity_curve[-1].cash_balance == pytest.approx(0 if len(expected_sides) == 2 else 107.02)
    if len(expected_sides) == 1:
        event = next(item for item in draft.events if item.event_code == "exit_fee_cash_shortfall")
        assert event.details["cash_shortfall"] == .01


def test_cash_blocked_exit_retries_at_next_sellable_open_without_rewriting_original_signal():
    draft = _simulate(retry=True)
    assert [(item.side, item.trade_date, item.price) for item in draft.trades] == [
        ("buy", "2026-07-02", 100), ("sell", "2026-07-06", 101),
    ]
    strategy = draft.strategies[0]
    assert strategy.status == "closed" and strategy.exit_reason == "horizon_close"
    assert strategy.pending_exit_reason is None and strategy.pending_exit_date is None
    assert strategy.error_message is None
    assert draft.equity_curve[-1].cash_balance == pytest.approx(92.83)
    assert [item.event_date for item in draft.events if item.event_code == "exit_fee_cash_shortfall"] == ["2026-07-03"]


@pytest.mark.parametrize("earlier_opening_proceeds", [True, False])
def test_fee_coverage_uses_only_cash_released_before_the_exit(earlier_opening_proceeds):
    first = _strategy(horizon_days=1, allocation_pct=50)
    second = _strategy(horizon_days=1, strategy_id=2, plan_id=11, symbol="000001", allocation_pct=50, target_price=110 if earlier_opening_proceeds else 200)
    other = _rows()
    other[-1] = _bar("2026-07-03", 120 if earlier_opening_proceeds else 100, 121, 99, 120)
    draft = simulate_paper_portfolio(
        _account_with_cash(2 * INITIAL_CASH), [first, second], {first.symbol: _rows(), second.symbol: other},
        as_of=datetime(2026, 7, 3, 16), cost_profile=STRESS_COST,
    )
    first_result = next(item for item in draft.strategies if item.strategy_id == first.id)
    assert first_result.status == ("closed" if earlier_opening_proceeds else "open")
    assert all(point.cash_balance >= 0 for point in draft.equity_curve)
    assert sum(item.side == "sell" for item in draft.trades) == (2 if earlier_opening_proceeds else 1)


def test_default_cost_profile_keeps_existing_fills_and_cash_conservation():
    draft = _simulate(cost=resolve_cost_profile("base"))
    buy, sell = draft.trades
    expected_cash = INITIAL_CASH - buy.gross_amount - buy.friction_amount + sell.gross_amount - sell.friction_amount
    assert draft.equity_curve[-1].cash_balance == pytest.approx(expected_cash, abs=.005)
    assert draft.strategies[0].status == "closed"
    assert not any(item.event_code == "exit_fee_cash_shortfall" for item in draft.events)


def test_default_minimum_commission_cannot_borrow_against_other_open_positions():
    first = _strategy(symbol="600519", allocation_pct=100, horizon_days=20)
    small = _strategy(
        strategy_id=2, plan_id=11, symbol="000001", allocation_pct=1, horizon_days=1,
        snapshot_price=.04, snapshot_anchor_close=.04, target_price=.05, stop_price=.03,
    )
    rows = {
        first.symbol: [
            _bar("2026-07-01", 100, 101, 99, 100),
            _bar("2026-07-02", 99.84, 100, 99, 99.84),
            _bar("2026-07-03", 99.84, 100, 99, 99.84),
        ],
        small.symbol: [_bar(day, .04, .041, .039, .04) for day in ("2026-07-01", "2026-07-02", "2026-07-03")],
    }
    draft = simulate_paper_portfolio(
        _account_with_cash(10_000.10), [first, small], rows, as_of=datetime(2026, 7, 3, 16),
    )
    assert [(item.side, item.quantity, item.friction_amount) for item in draft.trades] == [("buy", 100, 7.10), ("buy", 100, 5)]
    assert all(point.cash_balance == 0 for point in draft.equity_curve)
    assert all(item.status == "open" for item in draft.strategies)
    event = next(item for item in draft.events if item.event_code == "exit_fee_cash_shortfall")
    assert event.strategy_id == small.id and event.details["cash_shortfall"] == 1


def test_fee_admission_semantics_are_bound_into_simulation_fingerprint(monkeypatch):
    import app.services.paper_trading as service

    current = _simulate(cost=resolve_cost_profile("base"))
    original = service._simulation_configuration

    def without_exit_cash_contract(profile, initial_cash):
        value = original(profile, initial_cash)
        value.pop("exit_cash", None)
        return value

    monkeypatch.setattr(service, "_simulation_configuration", without_exit_cash_contract)
    missing_contract = _simulate(cost=resolve_cost_profile("base"))
    assert "exit_cash" in current.configuration
    assert current.input_fingerprint != missing_contract.input_fingerprint
    assert current.trades == missing_contract.trades


def test_public_runner_persists_unexecuted_exit_with_owned_shares_and_verifiable_history(tmp_path):
    path = tmp_path / "paper-exit-cash.sqlite3"
    cache = SQLiteCache(path)
    cache.update_paper_trading_account(PaperTradingAccountUpdate(initial_cash=INITIAL_CASH))
    plan = _review_plan().model_copy(update={"horizon_days": 1})
    _persist_review_plan_projection(path, plan)
    strategy = cache.create_paper_strategy(plan, _strategy_create(plan, allocation_pct=100), activation_market_time="2026-07-01 10:00:00")

    class SyntheticHub:
        def __init__(self):
            self.cache = cache

        async def kline(self, symbol, **kwargs):
            assert symbol == strategy.symbol
            return _rows()

    payload = PaperSimulationRequest(as_of=datetime(2026, 7, 3, 16), benchmark_symbol=None,
                                    cost_overrides=PaperCostOverrides(minimum_commission=10000))
    summary = asyncio.run(run_paper_simulation(SyntheticHub(), payload, now=datetime(2026, 7, 3, 16)))
    assert summary.execution_count == 1 and summary.closed_count == 0
    assert summary.dashboard.positions[0].quantity == 100
    stored = cache.paper_trading_run_export(summary.run_id)
    assert stored.strategies[0].status == "open"
    assert stored.strategies[0].pending_exit_reason == "horizon_close"
    assert stored.equity_curve[-1].cash_balance == 0
    assert stored.run.output_digest == summary.dashboard.latest_run.output_digest
    assert any(item.event_code == "exit_fee_cash_shortfall" for item in stored.events)
