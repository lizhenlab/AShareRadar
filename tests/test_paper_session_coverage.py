from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import sqlite3

import pytest

from app.services import paper_trading as service
from app.services import trading_calendar as calendar
from app.services.cache import SQLiteCache
from tests.test_api_paper_trading_routes import _client
from tests.test_paper_trading import (
    _account, _bar, _persist_review_plan_projection, _review_plan, _strategy, _strategy_create,
)


@pytest.fixture(autouse=True)
def bundled_calendar_only(monkeypatch):
    baseline, warning = calendar._load_calendar_file(
        calendar.BUNDLED_CALENDAR_PATH, calendar.TradeCalendarSource.BUNDLED_BASELINE,
    )
    assert baseline is not None, warning
    monkeypatch.setattr(calendar, "_trade_days", lambda: baseline)
    monkeypatch.setattr(calendar, "_should_auto_refresh", lambda *args: False)


def _rows():
    return [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 102, 99, 101),
        _bar("2026-07-03", 101, 104, 100, 103),
    ]


@pytest.mark.parametrize(("activation", "as_of"), [
    ("2026-07-03 10:00:00", datetime(2026, 7, 5, 16)),
    ("2026-07-04 10:00:00", datetime(2026, 7, 5, 16)),
    ("2026-10-01 10:00:00", datetime(2026, 10, 7, 16)),
])
def test_no_post_activation_exchange_session_keeps_strategy_pending(activation, as_of) -> None:
    strategy = _strategy(activation_market_time=activation, target_price=150, stop_price=50)
    rows = [
        _bar(day.isoformat(), 100, 101, 99, 100)
        for day in calendar.trading_dates_between(date(2026, 7, 1), service.completed_daily_bar_cutoff(as_of))
    ]
    draft = service.simulate_paper_portfolio(_account(), [strategy], {strategy.symbol: rows}, as_of=as_of)

    assert draft.strategies[0].status == "pending"
    assert draft.strategies[0].error_message is None
    assert draft.data_unavailable_count == 0
    assert draft.trades == [] and draft.equity_curve == []


@pytest.mark.parametrize(("activation", "as_of", "observed_through"), [
    ("2026-07-04 10:00:00", datetime(2026, 7, 6, 16), date(2026, 7, 3)),
    ("2026-10-01 10:00:00", datetime(2026, 10, 8, 16), date(2026, 9, 30)),
])
def test_missing_first_post_activation_exchange_session_stays_unavailable(activation, as_of, observed_through) -> None:
    strategy = _strategy(activation_market_time=activation, target_price=150, stop_price=50)
    rows = [
        _bar(day.isoformat(), 100, 101, 99, 100)
        for day in calendar.trading_dates_between(date(2026, 7, 1), observed_through)
    ]
    draft = service.simulate_paper_portfolio(_account(), [strategy], {strategy.symbol: rows}, as_of=as_of)

    assert draft.strategies[0].status == "data_unavailable"
    assert draft.strategies[0].error_message == "激活后没有可用的完整日K"
    assert draft.data_unavailable_count == 1
    assert draft.trades == [] and draft.equity_curve == []


@pytest.mark.parametrize("with_peer", [False, True])
def test_missing_active_strategy_tail_rejects_entire_account(with_peer: bool) -> None:
    first = _strategy(target_price=150, stop_price=50)
    strategies, rows = [first], {first.symbol: _rows()[:2]}
    if with_peer:
        second = _strategy(strategy_id=2, plan_id=11, symbol="000001", target_price=150, stop_price=50)
        strategies.append(second)
        rows[second.symbol] = _rows()
    with pytest.raises(ValueError, match="2026-07-03.*完整交易会话"):
        service.simulate_paper_portfolio(_account(), strategies, rows, as_of=datetime(2026, 7, 3, 16))


@pytest.mark.parametrize("terminal", ["closed", "skipped", "expired"])
def test_finished_strategy_does_not_require_future_prices(terminal: str) -> None:
    first = _strategy(target_price=150, stop_price=50)
    rows = _rows()
    if terminal == "closed":
        first = first.model_copy(update={"horizon_days": 1})
    elif terminal == "skipped":
        first = first.model_copy(update={"target_price": 99})
        rows = rows[:2]
    else:
        first = first.model_copy(update={"allocation_pct": .01, "entry_expiry_sessions": 1})
        rows = rows[:2]
    draft = service.simulate_paper_portfolio(
        _account(), [first], {first.symbol: rows}, as_of=datetime(2026, 7, 6, 16),
    )
    assert draft.strategies[0].status == terminal
    assert draft.data_unavailable_count == 0


@pytest.mark.parametrize("benchmark_length", [1, 2])
def test_missing_benchmark_session_does_not_fabricate_excess_returns(benchmark_length: int) -> None:
    strategy = _strategy(target_price=150, stop_price=50)
    rows = _rows()
    draft = service.simulate_paper_portfolio(
        _account(), [strategy], {strategy.symbol: rows}, as_of=datetime(2026, 7, 3, 16),
        benchmark_rows=rows[:benchmark_length],
    )
    assert draft.trades
    assert draft.benchmark_status == "unavailable"
    assert "完整交易会话" in (draft.benchmark_message or "")
    assert all(point.benchmark_equity is None and point.benchmark_return_pct is None
               and point.excess_return_pct is None for point in draft.equity_curve)


def test_explicit_suspension_preserves_inventory_and_known_benchmark_mark() -> None:
    strategy = _strategy(target_price=150, stop_price=50)
    rows = [*_rows()[:2], _bar("2026-07-03", 101, 101, 101, 101, volume=0)]
    draft = service.simulate_paper_portfolio(
        _account(), [strategy], {strategy.symbol: rows}, as_of=datetime(2026, 7, 3, 16), benchmark_rows=rows,
    )
    held = draft.strategies[0]
    assert held.status == "open" and held.quantity == 900 and held.last_price == 101
    assert [trade.side for trade in draft.trades] == ["buy"]
    assert draft.equity_curve[-1].market_value == held.quantity * 101
    assert draft.benchmark_status == "available"
    assert draft.equity_curve[-1].benchmark_return_pct == 1.0


def test_observed_suspended_benchmark_start_can_use_previous_close() -> None:
    strategy = _strategy(target_price=150, stop_price=50)
    benchmark = [_rows()[0], _bar("2026-07-02", 100, 100, 100, 100, volume=0), _rows()[2]]
    draft = service.simulate_paper_portfolio(
        _account(), [strategy], {strategy.symbol: _rows()}, as_of=datetime(2026, 7, 3, 16), benchmark_rows=benchmark,
    )
    assert draft.benchmark_status == "available"
    assert [point.benchmark_return_pct for point in draft.equity_curve] == [0.0, 3.0]


def test_complete_path_retains_execution_and_return_values() -> None:
    strategy = _strategy(target_price=150, stop_price=50)
    draft = service.simulate_paper_portfolio(
        _account(), [strategy], {strategy.symbol: _rows()}, as_of=datetime(2026, 7, 3, 16), benchmark_rows=_rows(),
    )
    assert [(trade.side, trade.trade_date, trade.quantity, trade.price) for trade in draft.trades] == [
        ("buy", "2026-07-02", 900, 100),
    ]
    last = draft.equity_curve[-1]
    assert last.total_equity == 1_002_569.6
    assert last.benchmark_return_pct == 3.0 and last.excess_return_pct == -2.743


def test_session_policy_changes_fingerprint_without_user_toggle(monkeypatch) -> None:
    current = service.simulate_paper_portfolio(_account(), [], {}, as_of=datetime(2026, 7, 3, 16))
    original = service._simulation_configuration

    def previous_configuration(profile, initial_cash):
        config = original(profile, initial_cash)
        config.pop("required_session_evidence", None)
        config.pop("benchmark_session_evidence", None)
        return config

    monkeypatch.setattr(service, "_simulation_configuration", previous_configuration)
    previous = service.simulate_paper_portfolio(_account(), [], {}, as_of=datetime(2026, 7, 3, 16))
    assert current.input_fingerprint != previous.input_fingerprint


def _paper_rows(path: Path) -> dict[str, list[tuple]]:
    tables = ("paper_trading_account", "paper_strategy", "paper_trading_run", "paper_strategy_result",
              "paper_trade", "paper_equity_snapshot", "paper_trading_event")
    with sqlite3.connect(path) as connection:
        return {table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in tables}


def test_rejected_run_is_400_and_preserves_saved_account_and_strategy(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "incomplete-paper.sqlite3"
    cache = SQLiteCache(path)
    plan = _review_plan()
    _persist_review_plan_projection(path, plan)
    cache.create_paper_strategy(plan, _strategy_create(plan, allocation_pct=10),
                                activation_market_time="2026-07-01 10:00:00")
    strategy = cache.paper_strategies()[0]
    rows = {strategy.symbol: _rows()[:2]}
    saved = cache.save_paper_simulation(service.simulate_paper_portfolio(
        cache.paper_trading_account(), [strategy], rows, as_of=datetime(2026, 7, 2, 16),
    ))
    assert saved.selected_run_id is not None
    exported = cache.paper_trading_run_export(saved.selected_run_id).model_dump()
    before = _paper_rows(path)

    async def market_data(*args):
        return rows, {}, {}

    async def benchmark_data(*args):
        return _rows(), None

    monkeypatch.setattr(service, "_paper_market_data", market_data)
    monkeypatch.setattr(service, "_benchmark_market_data", benchmark_data)
    response = _client(cache).post("/api/paper-trading/run", json={"as_of": "2026-07-03 16:00:00"})
    assert response.status_code == 400
    assert "2026-07-03" in response.json()["detail"]
    assert _paper_rows(path) == before
    assert cache.paper_trading_run_export(saved.selected_run_id).model_dump() == exported
