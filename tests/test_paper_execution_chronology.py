from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.services.paper_trading import simulate_paper_portfolio
from tests.test_paper_trading import _account, _bar, _strategy


@pytest.mark.parametrize("exit_kind", ["target", "horizon"])
def test_later_sale_proceeds_cannot_fund_same_session_open_purchase(exit_kind: str) -> None:
    first = _strategy(
        allocation_pct=100,
        target_price=110 if exit_kind == "target" else 150,
        horizon_days=20 if exit_kind == "target" else 1,
    )
    second = _strategy(
        strategy_id=2, plan_id=11, symbol="000001", allocation_pct=100,
        activation_market_time="2026-07-02 16:00:00", target_price=150,
    )
    draft = simulate_paper_portfolio(
        _account().model_copy(update={"initial_cash": 100_000}),
        [first, second],
        {
            first.symbol: [
                _bar("2026-07-01", 100, 101, 99, 100),
                _bar("2026-07-02", 100, 105, 99, 104),
                _bar("2026-07-03", 104, 112, 103, 111),
            ],
            second.symbol: [
                _bar("2026-07-01", 100, 101, 99, 100),
                _bar("2026-07-02", 100, 101, 99, 100),
                _bar("2026-07-03", 100, 105, 99, 104),
            ],
        },
        as_of=datetime(2026, 7, 3, 16),
    )

    assert [(trade.strategy_id, trade.side) for trade in draft.trades] == [(1, "buy"), (1, "sell")]
    assert draft.strategies[1].quantity == 0
    assert draft.equity_curve[-1].cash_balance > 100_000


@pytest.mark.parametrize(
    ("open_price", "expected_reason"), [(112, "target_hit"), (85, "stop_hit")],
)
def test_known_open_barrier_precedes_later_intraday_double_touch(open_price: float, expected_reason: str) -> None:
    strategy = _strategy()
    draft = simulate_paper_portfolio(
        _account(), [strategy],
        {strategy.symbol: [
            _bar("2026-07-01", 100, 101, 99, 100),
            _bar("2026-07-02", 100, 105, 99, 104),
            _bar("2026-07-03", open_price, 115, 80, 100),
        ]},
        as_of=datetime(2026, 7, 3, 16),
    )

    assert draft.trades[-1].price == open_price
    assert draft.strategies[0].exit_reason == expected_reason


def test_locked_exit_updates_mark_to_market_while_preserving_pending_signal() -> None:
    strategy = _strategy()
    locked = _bar("2026-07-03", 100, 100, 100, 100).model_copy(
        update={"open_execution_status": "locked_limit_down"},
    )
    draft = simulate_paper_portfolio(
        _account(), [strategy],
        {strategy.symbol: [
            _bar("2026-07-01", 100, 101, 99, 100),
            _bar("2026-07-02", 100, 112, 99, 111),
            locked,
        ]},
        as_of=datetime(2026, 7, 3, 16),
    )

    state = draft.strategies[0]
    assert state.status == "open"
    assert state.pending_exit_reason == "t1_deferred_target"
    assert state.last_price == 100
    assert draft.equity_curve[-1].market_value == state.quantity * 100
    assert [trade.side for trade in draft.trades] == ["buy"]


@pytest.mark.parametrize("exit_kind", ["pending", "target_gap", "stop_gap"])
def test_opening_sale_can_fund_opening_purchase_without_same_day_resale(exit_kind: str) -> None:
    first = _strategy(allocation_pct=100)
    second = _strategy(
        strategy_id=2, plan_id=11, symbol="000001", allocation_pct=100,
        activation_market_time="2026-07-02 16:00:00", target_price=110,
    )
    opening = {"pending": 104, "target_gap": 112, "stop_gap": 85}[exit_kind]
    draft = simulate_paper_portfolio(
        _account().model_copy(update={"initial_cash": 100_000}), [first, second],
        {
            first.symbol: [
                _bar("2026-07-01", 100, 101, 99, 100),
                _bar("2026-07-02", 100, 112 if exit_kind == "pending" else 105, 99, 104),
                _bar("2026-07-03", opening, 115, 80, 100),
            ],
            second.symbol: [
                _bar("2026-07-01", 100, 101, 99, 100),
                _bar("2026-07-02", 100, 101, 99, 100),
                _bar("2026-07-03", 100, 115, 80, 104),
            ],
        },
        as_of=datetime(2026, 7, 3, 16),
    )

    assert [(trade.strategy_id, trade.side) for trade in draft.trades] == [
        (1, "buy"), (1, "sell"), (2, "buy"),
    ]
    assert draft.trades[1].price == opening
    assert draft.strategies[0].held_sessions == 1
    assert draft.strategies[1].status == "open"
    assert draft.strategies[1].pending_exit_reason == "t1_deferred_ambiguous"
    assert draft.strategies[1].held_sessions == 0
    assert draft.equity_curve[-1].cash_balance >= 0


def test_new_execution_version_preserves_saved_legacy_run(tmp_path: Path, monkeypatch) -> None:
    from app.repositories import paper_trading as repository
    from app.services import paper_trading as service
    from app.services.cache import SQLiteCache

    cache = SQLiteCache(tmp_path / "version-boundary.db")
    account = cache.paper_trading_account()
    original_configuration = service._simulation_configuration

    def legacy_configuration(profile, initial_cash):
        result = original_configuration(profile, initial_cash)
        result.pop("session_order")
        result.pop("entry_cash")
        result["same_bar"] = "stop wins when target and stop are both touched"
        return result

    with monkeypatch.context() as old:
        old.setattr(service, "PAPER_TRADING_RULE_VERSION", "paper-review-plan-v2")
        old.setattr(repository, "PAPER_TRADING_RULE_VERSION", "paper-review-plan-v2")
        old.setattr(service, "_simulation_configuration", legacy_configuration)
        draft = simulate_paper_portfolio(account, [], {}, as_of=datetime(2026, 7, 3, 16))
        saved = cache.save_paper_simulation(draft)
        run_id = saved.selected_run_id
        assert run_id is not None
        original = cache.paper_trading_run_export(run_id).model_dump()

    current = simulate_paper_portfolio(account, [], {}, as_of=datetime(2026, 7, 3, 16))
    latest = cache.save_paper_simulation(current).latest_run
    assert latest is not None and latest.rule_version == "paper-review-plan-v3"
    assert current.input_fingerprint != draft.input_fingerprint
    assert cache.paper_trading_run_export(run_id).model_dump() == original
