from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.models.market import DAILY_KLINE_CONTRACT_VERSION, Kline
from app.db.advice_review_schema import _backfill_current_plan_revisions
from app.db.paper_trading_schema import paper_run_output_digest
from app.models.paper_trading import (
    PaperInstrumentMetadata,
    PaperStrategy,
    PaperStrategyCreate,
    PaperTradingAccount,
    PaperTradingAccountUpdate,
)
from app.models.reviews import AdviceReviewPlan
from app.services.cache import SQLiteCache
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs
from app.services.paper_trading import create_paper_strategy, simulate_paper_portfolio
from app.services.paper_trading_rules import resolve_trade_rule_profile


def test_paper_simulation_is_no_lookahead_and_deterministic() -> None:
    account = _account()
    strategy = _strategy(horizon_days=5)
    rows = [
        _bar("2026-07-01", 100, 110, 90, 100),
        _bar("2026-07-02", 100, 105, 99, 104),
        _bar("2026-07-03", 104, 112, 103, 111),
    ]

    first = simulate_paper_portfolio(
        account,
        [strategy],
        {strategy.symbol: rows},
        as_of=datetime(2026, 7, 3, 16),
    )
    second = simulate_paper_portfolio(
        account,
        [strategy],
        {strategy.symbol: rows},
        as_of=datetime(2026, 7, 3, 16),
    )

    assert first == second
    assert [item.side for item in first.trades] == ["buy", "sell"]
    assert first.trades[0].trade_date == "2026-07-02"
    assert first.trades[0].price == 100
    assert first.trades[0].quantity == 900
    assert first.trades[1].trade_date == "2026-07-03"
    assert first.trades[1].price == 110
    assert first.strategies[0].status == "closed"
    assert first.strategies[0].exit_reason == "target_hit"
    assert first.strategies[0].realized_pnl == pytest.approx(8_863.56)
    assert first.trades[0].commission_amount > 0
    assert first.trades[0].stamp_duty_amount == 0
    assert first.trades[1].stamp_duty_amount > 0
    assert first.input_fingerprint == second.input_fingerprint
    filled = [item for item in first.events if item.event_code in {"buy_filled", "sell_filled"}]
    assert filled
    assert all(item.details["daily_bar_model_limited"] is True for item in filled)


def test_paper_fingerprint_binds_normalized_data_and_benchmark_errors() -> None:
    strategy = _strategy()
    rows = {
        strategy.symbol: [
            _bar("2026-07-01", 100, 101, 99, 100),
            _bar("2026-07-02", 100, 102, 99, 101),
        ]
    }

    first = simulate_paper_portfolio(
        _account(),
        [strategy],
        rows,
        as_of=datetime(2026, 7, 2, 16),
        data_errors={strategy.symbol: "  provider   timeout  "},
        benchmark_error=" index   unavailable ",
    )
    equivalent = simulate_paper_portfolio(
        _account(),
        [strategy],
        rows,
        as_of=datetime(2026, 7, 2, 16),
        data_errors={strategy.symbol.lower(): "provider timeout"},
        benchmark_error="index unavailable",
    )
    changed_data_error = simulate_paper_portfolio(
        _account(),
        [strategy],
        rows,
        as_of=datetime(2026, 7, 2, 16),
        data_errors={strategy.symbol: "provider authentication failed"},
        benchmark_error="index unavailable",
    )
    changed_benchmark_error = simulate_paper_portfolio(
        _account(),
        [strategy],
        rows,
        as_of=datetime(2026, 7, 2, 16),
        data_errors={strategy.symbol: "provider timeout"},
        benchmark_error="index authentication failed",
    )
    changed_capital = simulate_paper_portfolio(
        _account().model_copy(update={"initial_cash": 2_000_000}),
        [strategy],
        rows,
        as_of=datetime(2026, 7, 2, 16),
        data_errors={strategy.symbol: "provider timeout"},
        benchmark_error="index unavailable",
    )
    remapped_ids = simulate_paper_portfolio(
        _account(),
        [strategy.model_copy(update={"id": 101, "plan_id": 110, "advice_id": 120})],
        rows,
        as_of=datetime(2026, 7, 2, 16),
        data_errors={strategy.symbol: "provider timeout"},
        benchmark_error="index unavailable",
    )

    assert first.input_fingerprint == equivalent.input_fingerprint
    assert first.market_data_hash == equivalent.market_data_hash
    assert first.input_fingerprint != changed_data_error.input_fingerprint
    assert first.input_fingerprint != changed_benchmark_error.input_fingerprint
    assert first.input_fingerprint != changed_capital.input_fingerprint
    assert first.input_fingerprint == remapped_ids.input_fingerprint


def test_entry_day_double_barrier_is_deferred_by_t1_and_exits_next_session_open() -> None:
    strategy = _strategy()
    rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 112, 89, 105),
        _bar("2026-07-03", 85, 90, 80, 88),
    ]

    draft = simulate_paper_portfolio(
        _account(),
        [strategy],
        {strategy.symbol: rows},
        as_of=datetime(2026, 7, 3, 16),
    )

    assert [item.trade_date for item in draft.trades] == ["2026-07-02", "2026-07-03"]
    assert draft.strategies[0].exit_reason == "t1_deferred_ambiguous"
    assert draft.trades[-1].price == 85
    assert any(item.event_code == "t1_deferred_ambiguous" for item in draft.events)


def test_entry_day_target_signal_stays_open_until_next_completed_bar() -> None:
    strategy = _strategy()
    draft = simulate_paper_portfolio(
        _account(),
        [strategy],
        {
            strategy.symbol: [
                _bar("2026-07-01", 100, 101, 99, 100),
                _bar("2026-07-02", 100, 111, 99, 109),
            ]
        },
        as_of=datetime(2026, 7, 2, 16),
    )

    assert [item.side for item in draft.trades] == ["buy"]
    assert draft.strategies[0].status == "open"
    assert draft.strategies[0].pending_exit_reason == "t1_deferred_target"


def test_t1_deferred_exit_waits_through_next_session_suspension() -> None:
    strategy = _strategy(target_price=110, stop_price=90)
    draft = simulate_paper_portfolio(
        _account(),
        [strategy],
        {
            strategy.symbol: [
                _bar("2026-07-01", 100, 101, 99, 100),
                _bar("2026-07-02", 100, 112, 99, 110),
                _bar("2026-07-03", 110, 110, 110, 110, volume=0),
                _bar("2026-07-06", 106, 108, 104, 107),
            ]
        },
        as_of=datetime(2026, 7, 6, 16),
    )

    assert [item.trade_date for item in draft.trades] == ["2026-07-02", "2026-07-06"]
    assert draft.trades[-1].price == 106
    assert draft.strategies[0].exit_reason == "t1_deferred_target"
    assert any(item.event_code == "exit_execution_metadata_unavailable" for item in draft.events)


def test_zero_volume_blocks_entry_and_records_event() -> None:
    strategy = _strategy(entry_expiry_sessions=3)
    rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 101, 99, 100, volume=0),
        _bar("2026-07-03", 101, 105, 100, 104),
    ]

    draft = simulate_paper_portfolio(
        _account(),
        [strategy],
        {strategy.symbol: rows},
        as_of=datetime(2026, 7, 3, 16),
    )

    assert draft.trades[0].trade_date == "2026-07-03"
    assert any(item.event_code == "execution_metadata_unavailable" for item in draft.events)


@pytest.mark.parametrize(
    "execution_update",
    [
        {
            "volume": 0,
            "session_status": "suspended",
            "open_execution_status": "unavailable",
        },
        {"open_execution_status": "unavailable"},
    ],
)
def test_suspended_pending_entry_does_not_interpret_carried_ohlc_as_a_barrier(
    execution_update: dict[str, object],
) -> None:
    strategy = _strategy(entry_expiry_sessions=3)
    unavailable = _bar("2026-07-02", 500, 1_000, 1, 500).model_copy(update=execution_update)
    draft = simulate_paper_portfolio(
        _account(),
        [strategy],
        {
            strategy.symbol: [
                _bar("2026-07-01", 100, 101, 99, 100),
                unavailable,
                _bar("2026-07-03", 100, 102, 99, 101),
            ]
        },
        as_of=datetime(2026, 7, 3, 16),
    )

    assert [item.trade_date for item in draft.trades] == ["2026-07-03"]
    assert draft.strategies[0].status == "open"
    assert draft.strategies[0].last_price == 101
    assert not any(item.event_code in {"target_before_entry", "invalid_before_entry"} for item in draft.events)


def test_entry_wait_expiry_is_configurable_and_does_not_create_a_fill() -> None:
    strategy = _strategy(
        entry_expiry_sessions=2,
        target_price=150,
        stop_price=50,
    )
    draft = simulate_paper_portfolio(
        _account(),
        [strategy],
        {
            strategy.symbol: [
                _bar("2026-07-01", 100, 101, 99, 100),
                _bar("2026-07-02", 100, 101, 99, 100, volume=0),
                _bar("2026-07-03", 101, 102, 100, 101, volume=0),
                _bar("2026-07-06", 102, 103, 101, 102),
            ]
        },
        as_of=datetime(2026, 7, 6, 16),
    )

    assert draft.trades == []
    assert draft.strategies[0].status == "expired"
    assert draft.strategies[0].exit_reason == "entry_expired"
    assert draft.strategies[0].entry_wait_sessions == 2
    assert any(item.event_code == "entry_expired" for item in draft.events)


def test_paper_simulation_rejects_conflicting_duplicate_daily_bars_per_strategy() -> None:
    strategy = _strategy()
    rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 102, 99, 101),
        _bar("2026-07-02", 100, 112, 99, 111),
    ]

    draft = simulate_paper_portfolio(
        _account(),
        [strategy],
        {strategy.symbol: rows},
        as_of=datetime(2026, 7, 2, 16),
    )

    assert draft.trades == []
    assert draft.strategies[0].status == "data_unavailable"
    assert any(item.event_code == "conflicting_daily_bar" for item in draft.events)


def test_paper_simulation_rejects_duplicate_with_conflicting_execution_metadata() -> None:
    strategy = _strategy()
    first = _bar("2026-07-02", 100, 102, 99, 101)
    rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        first,
        first.model_copy(update={"open_execution_status": "locked_limit_up"}),
    ]

    draft = simulate_paper_portfolio(_account(), [strategy], {strategy.symbol: rows}, as_of=datetime(2026, 7, 2, 16))

    assert draft.strategies[0].status == "data_unavailable"
    assert any(item.event_code == "conflicting_daily_bar" for item in draft.events)


def test_paper_simulation_fails_closed_without_pit_execution_evidence() -> None:
    strategy = _strategy()
    rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 102, 99, 101).model_copy(
            update={
                "point_in_time": False,
                "session_status": "unknown",
                "open_execution_status": "unknown",
                "corporate_action_status": "unknown",
                "execution_metadata_version": None,
            }
        ),
    ]

    draft = simulate_paper_portfolio(_account(), [strategy], {strategy.symbol: rows}, as_of=datetime(2026, 7, 2, 16))

    assert draft.trades == []
    assert draft.strategies[0].status == "data_unavailable"
    assert any(item.event_code == "execution_evidence_unavailable" for item in draft.events)


def test_paper_simulation_rejects_a_non_trading_day_bar_in_the_source_window() -> None:
    strategy = _strategy()
    rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 101, 99, 100, volume=0),
        _bar("2026-07-03", 100, 101, 99, 100, volume=0),
        _bar("2026-07-04", 100, 102, 99, 101),
        _bar("2026-07-06", 101, 103, 100, 102),
    ]

    draft = simulate_paper_portfolio(
        _account(),
        [strategy],
        {strategy.symbol: rows},
        as_of=datetime(2026, 7, 6, 16),
    )

    assert draft.trades == []
    assert draft.strategies[0].status == "data_unavailable"
    assert any(item.event_code == "execution_evidence_unavailable" for item in draft.events)


def test_paper_simulation_ignores_conflicting_daily_bars_after_as_of() -> None:
    strategy = _strategy(target_price=150, stop_price=50)
    rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 101, 99, 100, volume=0),
        _bar("2026-07-03", 100, 101, 99, 100, volume=0),
        _bar("2026-07-06", 101, 103, 100, 102),
        _bar("2026-07-07", 102, 104, 101, 103),
        _bar("2026-07-07", 110, 112, 109, 111),
    ]

    draft = simulate_paper_portfolio(
        _account(),
        [strategy],
        {strategy.symbol: rows},
        as_of=datetime(2026, 7, 6, 16),
    )

    assert [item.trade_date for item in draft.trades] == ["2026-07-06"]
    assert draft.strategies[0].status == "open"
    assert draft.data_end_date == "2026-07-06"


def test_locked_limit_up_blocks_buy_and_locked_limit_down_delays_sell() -> None:
    metadata = {"600519": _metadata("600519")}
    waiting = _strategy(
        strategy_id=1,
        activation_market_time="2026-07-06 10:00:00",
        target_price=150,
        stop_price=50,
    )
    waiting_rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 101, 99, 100, volume=0),
        _bar("2026-07-03", 100, 101, 99, 100, volume=0),
        _bar("2026-07-06", 100, 101, 99, 100),
        _bar("2026-07-07", 110, 110, 110, 110),
        _bar("2026-07-08", 108, 112, 105, 110),
    ]
    waiting_draft = simulate_paper_portfolio(
        _account(),
        [waiting],
        {waiting.symbol: waiting_rows},
        as_of=datetime(2026, 7, 8, 16),
        metadata_by_symbol=metadata,
    )

    assert waiting_draft.trades[0].trade_date == "2026-07-08"
    assert any(item.event_code == "locked_limit_up" for item in waiting_draft.events)

    exiting = _strategy(
        strategy_id=2,
        activation_market_time="2026-07-06 10:00:00",
        target_price=150,
        stop_price=95,
    )
    exit_rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 101, 99, 100, volume=0),
        _bar("2026-07-03", 100, 101, 99, 100, volume=0),
        _bar("2026-07-06", 100, 101, 99, 100),
        _bar("2026-07-07", 100, 102, 99, 101),
        _bar("2026-07-08", 90.9, 90.9, 90.9, 90.9),
        _bar("2026-07-09", 85, 89, 82, 86),
    ]
    exit_draft = simulate_paper_portfolio(
        _account(),
        [exiting],
        {exiting.symbol: exit_rows},
        as_of=datetime(2026, 7, 9, 16),
        metadata_by_symbol=metadata,
    )

    assert [item.trade_date for item in exit_draft.trades] == ["2026-07-07", "2026-07-09"]
    assert exit_draft.trades[-1].price == 85
    assert any(item.event_code == "exit_locked_limit_down" for item in exit_draft.events)


def test_board_profiles_and_star_quantity_do_not_use_universal_hundred_share_lot() -> None:
    trade_date = datetime(2026, 7, 10).date()
    legacy_date = datetime(2026, 7, 3).date()
    metadata = _metadata("688001")
    star = resolve_trade_rule_profile("688001", trade_date, metadata)
    main = resolve_trade_rule_profile("600519", trade_date, _metadata("600519"))
    chinext = resolve_trade_rule_profile("300750", trade_date, _metadata("300750"))
    bse = resolve_trade_rule_profile("920066.BJ", trade_date, _metadata("920066.BJ"))

    assert (main.min_buy_quantity, main.buy_quantity_step, main.price_limit_pct) == (100, 100, 10)
    assert (star.min_buy_quantity, star.buy_quantity_step, star.price_limit_pct) == (200, 1, 20)
    assert (chinext.min_buy_quantity, chinext.price_limit_pct) == (100, 20)
    assert (bse.min_buy_quantity, bse.buy_quantity_step, bse.price_limit_pct) == (100, 1, 30)
    assert resolve_trade_rule_profile("600519", legacy_date, _metadata("600519")).profile_id == "sse-main-2023"
    assert main.profile_id == "sse-main-2026"
    assert resolve_trade_rule_profile("300750", legacy_date, _metadata("300750")).profile_id == "szse-chinext-2023"
    assert chinext.profile_id == "szse-chinext-2026"
    assert resolve_trade_rule_profile("920066.BJ", legacy_date, _metadata("920066.BJ")).profile_id == "bse-main-2021"
    assert bse.profile_id == "bse-main-2026"

    strategy = _strategy(
        symbol="688001",
        target_price=100,
        stop_price=10,
        snapshot_price=40,
        snapshot_anchor_close=40,
    )
    draft = simulate_paper_portfolio(
        _account(),
        [strategy],
        {
            strategy.symbol: [
                _bar("2026-07-01", 40, 41, 39, 40),
                _bar("2026-07-02", 40, 41, 39, 40),
            ]
        },
        as_of=datetime(2026, 7, 2, 16),
        metadata_by_symbol={strategy.symbol: _metadata(strategy.symbol)},
    )

    assert draft.trades[0].quantity >= 200
    assert draft.trades[0].quantity % 100 != 0


def test_future_st_status_is_not_applied_to_an_earlier_trade_date() -> None:
    metadata = _metadata("600519").model_copy(update={"is_st": True, "status_effective_date": "2026-07-10"})

    profile = resolve_trade_rule_profile(
        "600519",
        datetime(2026, 7, 8).date(),
        metadata,
    )

    assert profile.price_limit_pct == 10
    assert profile.quality == "degraded"
    assert "historical_st_status_unknown" in profile.degradation_reasons


def test_price_limit_listing_session_count_is_safe_at_calendar_left_boundary() -> None:
    metadata = _metadata("600519").model_copy(update={"list_date": "1990-12-19"})

    profile = resolve_trade_rule_profile("600519", datetime(2026, 8, 12).date(), metadata)

    assert profile.price_limit_pct == 10
    assert "trading_calendar_out_of_coverage" not in profile.degradation_reasons


def test_price_limit_still_fails_closed_for_listing_before_calendar_coverage() -> None:
    metadata = _metadata("600519").model_copy(update={"list_date": "1990-12-18"})

    profile = resolve_trade_rule_profile("600519", datetime(2026, 8, 12).date(), metadata)

    assert profile.price_limit_pct is None
    assert "trading_calendar_out_of_coverage" in profile.degradation_reasons


def test_waiting_strategy_is_invalidated_and_priority_order_is_not_database_id_order() -> None:
    high_priority = _strategy(
        strategy_id=20,
        symbol="600519",
        allocation_pct=100,
        priority=10,
        target_price=150,
        stop_price=50,
    )
    low_priority = _strategy(
        strategy_id=1,
        plan_id=11,
        symbol="000001",
        allocation_pct=100,
        priority=0,
        target_price=110,
        stop_price=90,
    )
    rows = {
        high_priority.symbol: [
            _bar("2026-07-01", 100, 101, 99, 100),
            _bar("2026-07-02", 100, 102, 99, 101),
        ],
        low_priority.symbol: [
            _bar("2026-07-01", 100, 101, 99, 100),
            _bar("2026-07-02", 100, 111, 99, 110),
        ],
    }

    draft = simulate_paper_portfolio(
        _account(),
        [low_priority, high_priority],
        rows,
        as_of=datetime(2026, 7, 2, 16),
    )

    assert draft.trades[0].strategy_id == high_priority.id
    low_result = next(item for item in draft.strategies if item.strategy_id == low_priority.id)
    assert low_result.status == "skipped"
    assert low_result.exit_reason == "target_before_entry"
    assert [item.allocation_order for item in draft.strategies] == [1, 2]


def test_cost_profiles_are_split_and_monotonic() -> None:
    gross = 100_000
    base = trade_costs(resolve_cost_profile("base"), side="sell", gross_amount=gross)
    conservative = trade_costs(resolve_cost_profile("conservative"), side="sell", gross_amount=gross)
    stress = trade_costs(resolve_cost_profile("stress"), side="sell", gross_amount=gross)

    assert base.commission > 0
    assert base.stamp_duty > 0
    assert base.transfer_fee > 0
    assert base.slippage > 0
    assert base.total < conservative.total < stress.total


def test_repository_appends_immutable_runs_and_same_inputs_keep_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "paper-runs.db"
    cache = SQLiteCache(path)
    plan = _review_plan()
    _persist_review_plan_projection(path, plan)
    cache.create_paper_strategy(
        plan,
        _strategy_create(plan, allocation_pct=10),
        activation_market_time="2026-07-01 10:00:00",
    )
    account = cache.paper_trading_account()
    strategy = cache.paper_strategies()[0]
    rows = {
        strategy.symbol: [
            _bar("2026-07-01", 100, 101, 99, 100),
            _bar("2026-07-02", 100, 105, 99, 104),
            _bar("2026-07-03", 104, 112, 103, 111),
        ]
    }
    first = simulate_paper_portfolio(account, [strategy], rows, as_of=datetime(2026, 7, 3, 16))
    second = simulate_paper_portfolio(account, [strategy], rows, as_of=datetime(2026, 7, 3, 16))

    first_dashboard = cache.save_paper_simulation(first)
    second_dashboard = cache.save_paper_simulation(second)
    first_run_id = first_dashboard.selected_run_id
    second_run_id = second_dashboard.selected_run_id

    assert first_run_id and second_run_id and first_run_id != second_run_id
    assert first.input_fingerprint == second.input_fingerprint
    assert len(cache.paper_trading_runs()) == 2
    first_export = cache.paper_trading_run_export(first_run_id)
    second_export = cache.paper_trading_run_export(second_run_id)
    assert [item.model_dump(exclude={"id", "run_id", "created_at"}) for item in first_export.trades] == [
        item.model_dump(exclude={"id", "run_id", "created_at"}) for item in second_export.trades
    ]
    with pytest.raises(ValueError, match="不可变历史运行"):
        cache.delete_pending_paper_strategy(strategy.id)


def test_empty_run_freezes_initial_cash_and_blocks_later_account_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "paper-empty-run-account.db"
    cache = SQLiteCache(path)
    account = cache.paper_trading_account()
    draft = simulate_paper_portfolio(account, [], {}, as_of=datetime(2026, 7, 3, 16))
    saved = cache.save_paper_simulation(draft)
    run_id = saved.selected_run_id
    assert run_id is not None
    assert saved.performance.total_equity == 1_000_000

    with pytest.raises(ValueError, match="已有模拟策略或运行"):
        cache.update_paper_trading_account(PaperTradingAccountUpdate(initial_cash=2_000_000))

    with sqlite3.connect(path) as conn:
        digest = str(conn.execute("SELECT output_digest FROM paper_trading_run WHERE id = ?", (run_id,)).fetchone()[0])
        conn.execute("UPDATE paper_trading_account SET initial_cash = 2000000 WHERE id = 1")
    historical = cache.paper_trading_dashboard(run_id=run_id)
    assert historical.account.initial_cash == 2_000_000
    assert historical.performance.total_equity == 1_000_000
    assert historical.latest_run is not None
    assert historical.latest_run.configuration["initial_cash"] == 1_000_000
    assert historical.latest_run.output_digest == digest


def test_paper_run_read_fails_closed_after_sql_output_tampering(tmp_path: Path) -> None:
    path = tmp_path / "paper-run-tamper.db"
    cache = SQLiteCache(path)
    plan = _review_plan()
    _persist_review_plan_projection(path, plan)
    cache.create_paper_strategy(
        plan,
        _strategy_create(plan, allocation_pct=10),
        activation_market_time="2026-07-01 10:00:00",
    )
    strategy = cache.paper_strategies()[0]
    draft = simulate_paper_portfolio(
        cache.paper_trading_account(),
        [strategy],
        {
            strategy.symbol: [
                _bar("2026-07-01", 100, 101, 99, 100),
                _bar("2026-07-02", 100, 105, 99, 104),
                _bar("2026-07-03", 104, 112, 103, 111),
            ]
        },
        as_of=datetime(2026, 7, 3, 16),
    )
    dashboard = cache.save_paper_simulation(draft)
    run_id = dashboard.selected_run_id
    assert run_id is not None
    assert dashboard.latest_run is not None
    assert len(dashboard.latest_run.output_digest) == 64

    with sqlite3.connect(path) as conn:
        original_price = float(
            conn.execute(
                "SELECT price FROM paper_trade WHERE run_id = ? ORDER BY id LIMIT 1",
                (run_id,),
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE paper_trade SET price = price + 1 WHERE run_id = ?",
            (run_id,),
        )
    with pytest.raises(ValueError, match="输出账本校验失败"):
        cache.paper_trading_run_export(run_id)

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE paper_trade SET price = ? WHERE run_id = ?",
            (original_price, run_id),
        )
        conn.execute(
            "UPDATE paper_trading_run SET message = 'tampered' WHERE id = ?",
            (run_id,),
        )
    with pytest.raises(ValueError, match="输出账本校验失败"):
        cache.paper_trading_dashboard(run_id=run_id)


def test_paper_run_digest_rejects_trade_without_same_run_strategy_result(tmp_path: Path) -> None:
    path = tmp_path / "paper-orphan-trade.db"
    cache = SQLiteCache(path)
    empty = simulate_paper_portfolio(cache.paper_trading_account(), [], {}, as_of=datetime(2026, 7, 3, 16))
    run_id = cache.save_paper_simulation(empty).selected_run_id
    assert run_id is not None
    plan = _review_plan()
    _persist_review_plan_projection(path, plan)
    strategy = cache.create_paper_strategy(
        plan,
        _strategy_create(plan, allocation_pct=10),
        activation_market_time="2026-07-04 10:00:00",
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO paper_trade (
                run_id, strategy_id, symbol, side, trade_date, price, quantity,
                gross_amount, friction_amount, reason, created_at
            ) VALUES (?, ?, ?, 'buy', '2026-07-04', 100, 100, 10000, 0, 'tampered', ?)
            """,
            (run_id, strategy.id, strategy.symbol, "2026-07-04T08:00:00.000000Z"),
        )

    with pytest.raises(ValueError, match="同一运行|声明计数"):
        cache.paper_trading_dashboard(run_id=run_id)
    with pytest.raises(ValueError, match="同一运行|声明计数"):
        cache.paper_trading_run_export(run_id)


def test_paper_output_digest_is_stable_across_pure_surrogate_id_remap(tmp_path: Path) -> None:
    path = tmp_path / "paper-run-remap.db"
    cache = SQLiteCache(path)
    plan = _review_plan()
    _persist_review_plan_projection(path, plan)
    saved_strategy = cache.create_paper_strategy(
        plan,
        _strategy_create(plan, allocation_pct=10),
        activation_market_time="2026-07-01 10:00:00",
    )
    draft = simulate_paper_portfolio(
        cache.paper_trading_account(),
        [saved_strategy],
        {
            saved_strategy.symbol: [
                _bar("2026-07-01", 100, 101, 99, 100),
                _bar("2026-07-02", 100, 105, 99, 104),
                _bar("2026-07-03", 104, 112, 103, 111),
            ]
        },
        as_of=datetime(2026, 7, 3, 16),
    )
    dashboard = cache.save_paper_simulation(draft)
    run_id = dashboard.selected_run_id
    assert run_id is not None

    with sqlite3.connect(path) as conn:
        original = paper_run_output_digest(conn, run_id)
        remapped_run_id = run_id + 100
        remapped_strategy_id = saved_strategy.id + 100
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in (
            "paper_strategy_result",
            "paper_trade",
            "paper_trading_event",
        ):
            conn.execute(
                f"""
                UPDATE {table}
                SET run_id = ?,
                    strategy_id = CASE WHEN strategy_id IS NULL THEN NULL ELSE ? END
                WHERE run_id = ?
                """,
                (remapped_run_id, remapped_strategy_id, run_id),
            )
            conn.execute(f"UPDATE {table} SET id = id + 100 WHERE run_id = ?", (remapped_run_id,))
        conn.execute(
            "UPDATE paper_equity_snapshot SET run_id = ?, id = id + 100 WHERE run_id = ?",
            (remapped_run_id, run_id),
        )
        conn.execute(
            """
            UPDATE paper_strategy
            SET id = ?, plan_id = plan_id + 100, advice_id = advice_id + 100
            WHERE id = ?
            """,
            (remapped_strategy_id, saved_strategy.id),
        )
        conn.execute(
            "UPDATE paper_trading_run SET id = ? WHERE id = ?",
            (remapped_run_id, run_id),
        )

        assert paper_run_output_digest(conn, remapped_run_id) == original


def test_benchmark_and_excess_returns_are_persisted_and_reported(tmp_path: Path) -> None:
    path = tmp_path / "paper-benchmark.db"
    cache = SQLiteCache(path)
    plan = _review_plan()
    _persist_review_plan_projection(path, plan)
    cache.create_paper_strategy(
        plan,
        _strategy_create(plan, allocation_pct=10),
        activation_market_time="2026-07-01 10:00:00",
    )
    account = cache.paper_trading_account()
    strategy = cache.paper_strategies()[0].model_copy(update={"target_price": 150, "stop_price": 50})
    strategy_rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 102, 99, 101),
        _bar("2026-07-03", 101, 104, 100, 103),
    ]
    benchmark_rows = [
        _bar("2026-07-01", 100, 100, 100, 100),
        _bar("2026-07-02", 101, 101, 101, 101),
        _bar("2026-07-03", 105, 105, 105, 105),
    ]
    draft = simulate_paper_portfolio(
        account,
        [strategy],
        {strategy.symbol: strategy_rows},
        benchmark_symbol="000300.SH",
        benchmark_rows=benchmark_rows,
        as_of=datetime(2026, 7, 3, 16),
    )

    dashboard = cache.save_paper_simulation(draft)
    latest = dashboard.equity_curve[-1]

    assert latest.benchmark_return_pct == pytest.approx((105 / 101 - 1) * 100, abs=1e-4)
    assert latest.excess_return_pct == pytest.approx(
        latest.return_pct - latest.benchmark_return_pct,
        abs=1e-4,
    )
    assert dashboard.performance.benchmark_return_pct == latest.benchmark_return_pct
    assert dashboard.performance.excess_return_pct == latest.excess_return_pct
    assert dashboard.performance.cost_to_gross_profit_pct is not None
    assert dashboard.performance.cost_to_gross_profit_pct > 0


def test_benchmark_return_starts_at_first_trade_day_open_not_its_close() -> None:
    strategy = _strategy(target_price=150, stop_price=50)
    strategy_rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 102, 99, 101),
    ]
    benchmark_rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 110, 111, 99, 100),
    ]

    draft = simulate_paper_portfolio(
        _account(),
        [strategy],
        {strategy.symbol: strategy_rows},
        benchmark_symbol="000300.SH",
        benchmark_rows=benchmark_rows,
        as_of=datetime(2026, 7, 2, 16),
    )

    point = draft.equity_curve[0]
    assert point.benchmark_return_pct == pytest.approx((100 / 110 - 1) * 100, abs=1e-4)
    assert point.benchmark_equity == pytest.approx(_account().initial_cash * 100 / 110, abs=0.01)


def test_conflicting_benchmark_bars_degrade_benchmark_without_changing_trades() -> None:
    strategy = _strategy(target_price=150, stop_price=50)
    strategy_rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 102, 99, 101),
    ]
    benchmark_rows = [
        _bar("2026-07-01", 100, 101, 99, 100),
        _bar("2026-07-02", 100, 102, 99, 101),
        _bar("2026-07-02", 100, 112, 99, 111),
    ]

    draft = simulate_paper_portfolio(
        _account(),
        [strategy],
        {strategy.symbol: strategy_rows},
        benchmark_symbol="000300.SH",
        benchmark_rows=benchmark_rows,
        as_of=datetime(2026, 7, 2, 16),
    )

    assert draft.trades
    assert draft.benchmark_status == "unavailable"
    assert "冲突日K" in (draft.benchmark_message or "")


def test_paper_repository_freezes_plan_and_rejects_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "paper.db"
    cache = SQLiteCache(path)
    plan = _review_plan()
    _persist_review_plan_projection(path, plan)
    payload = _strategy_create(plan, allocation_pct=20)

    saved = cache.create_paper_strategy(
        plan,
        payload,
        activation_market_time="2026-07-01 10:00:00",
    )

    assert saved.symbol == plan.symbol
    assert saved.target_price == plan.target_price
    assert saved.allocation_pct == 20
    with pytest.raises(ValueError, match="已加入模拟交易"):
        cache.create_paper_strategy(
            plan,
            payload,
            activation_market_time="2026-07-01 11:00:00",
        )
    with pytest.raises(ValueError, match="不能修改初始资金"):
        cache.update_paper_trading_account(PaperTradingAccountUpdate(initial_cash=2_000_000))


def test_paper_repository_rechecks_plan_revision_inside_write_transaction(tmp_path: Path) -> None:
    path = tmp_path / "paper-cas.db"
    cache = SQLiteCache(path)
    stale_plan = _review_plan()
    _persist_review_plan_projection(path, stale_plan)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE advice_review_plan SET revision = 2, plan_payload_digest = ? WHERE id = ?",
            ("b" * 64, stale_plan.id),
        )

    with pytest.raises(ValueError, match="版本已变化"):
        cache.create_paper_strategy(
            stale_plan,
            _strategy_create(stale_plan, allocation_pct=20),
            activation_market_time="2026-07-01 10:00:00",
        )

    assert cache.paper_strategies() == []


def test_paper_repository_rejects_noncanonical_plan_ledger_payload(tmp_path: Path) -> None:
    path = tmp_path / "paper-ledger-payload.db"
    cache = SQLiteCache(path)
    plan = _review_plan()
    _persist_review_plan_projection(path, plan)
    with sqlite3.connect(path) as conn:
        payload_json = str(
            conn.execute(
                """
                SELECT payload_json FROM advice_review_plan_revision
                WHERE plan_id = ? AND revision = ?
                """,
                (plan.id, plan.revision),
            ).fetchone()[0]
        )
        conn.execute(
            """
            UPDATE advice_review_plan_revision SET payload_json = ?
            WHERE plan_id = ? AND revision = ?
            """,
            (f" {payload_json}", plan.id, plan.revision),
        )

    with pytest.raises(ValueError, match="修订账本"):
        cache.create_paper_strategy(
            plan,
            _strategy_create(plan, allocation_pct=20),
            activation_market_time="2026-07-01 10:00:00",
        )

    assert cache.paper_strategies() == []


def test_paper_strategy_read_rebinds_frozen_fields_to_plan_revision(tmp_path: Path) -> None:
    path = tmp_path / "paper-strategy-tamper.db"
    cache = SQLiteCache(path)
    plan = _review_plan()
    _persist_review_plan_projection(path, plan)
    saved = cache.create_paper_strategy(
        plan,
        _strategy_create(plan, allocation_pct=20),
        activation_market_time="2026-07-01 10:00:00",
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE paper_strategy SET target_price = target_price + 1 WHERE id = ?",
            (saved.id,),
        )

    with pytest.raises(ValueError, match="冻结计划修订账本不一致"):
        cache.paper_strategies()


def test_paper_strategy_create_rejects_stale_plan_revision_or_digest() -> None:
    plan = _review_plan()
    cache = SimpleNamespace(advice_review_plan=lambda _plan_id: plan)

    with pytest.raises(ValueError, match="版本已变化"):
        create_paper_strategy(
            cache,
            PaperStrategyCreate(
                plan_id=plan.id,
                expected_plan_revision=plan.revision + 1,
                expected_plan_payload_digest=plan.plan_payload_digest,
            ),
            now=datetime(2026, 7, 1, 16),
        )


def test_paper_tables_are_in_runtime_diagnostics(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "paper.db")

    counts = cache.table_counts()

    assert counts["paper_trading_account"] == 1
    assert counts["paper_strategy"] == 0
    assert counts["paper_trade"] == 0


def _account() -> PaperTradingAccount:
    return PaperTradingAccount(
        name="测试模拟账户",
        initial_cash=1_000_000,
        modelled_one_way_friction_pct=0.05,
        created_at="2026-07-01T00:00:00.000000Z",
        updated_at="2026-07-01T00:00:00.000000Z",
    )


def _strategy(
    *,
    horizon_days: int = 20,
    strategy_id: int = 1,
    plan_id: int = 10,
    symbol: str = "600519",
    activation_market_time: str = "2026-07-01 10:00:00",
    allocation_pct: float = 10,
    priority: int = 0,
    entry_expiry_sessions: int = 5,
    target_price: float = 110,
    stop_price: float = 90,
    snapshot_price: float = 100,
    snapshot_anchor_close: float = 100,
) -> PaperStrategy:
    return PaperStrategy(
        id=strategy_id,
        plan_id=plan_id,
        plan_revision=1,
        plan_payload_digest="a" * 64,
        advice_id=20,
        symbol=symbol,
        activation_market_time=activation_market_time,
        allocation_pct=allocation_pct,
        priority=priority,
        entry_expiry_sessions=entry_expiry_sessions,
        snapshot_market_time="2026-07-01 09:45:00",
        snapshot_price=snapshot_price,
        snapshot_adjustment_mode="qfq",
        snapshot_anchor_date="2026-07-01",
        snapshot_anchor_close=snapshot_anchor_close,
        snapshot_data_version="snapshot-v1",
        snapshot_contract_version=DAILY_KLINE_CONTRACT_VERSION,
        target_price=target_price,
        stop_price=stop_price,
        horizon_days=horizon_days,
        status="pending",
        created_at="2026-07-01T00:00:00.000000Z",
        updated_at="2026-07-01T00:00:00.000000Z",
    )


def _bar(
    day: str,
    open_price: float,
    high: float,
    low: float,
    close: float,
    *,
    volume: float = 1_000,
) -> Kline:
    return Kline(
        date=day,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        adjustment_mode="qfq",
        data_version="paper-test-v1",
        contract_version=DAILY_KLINE_CONTRACT_VERSION,
        as_of=f"{day} 15:15:00",
        point_in_time=True,
        session_status="trading" if volume > 0 else "suspended",
        open_execution_status="tradable" if volume > 0 else "unavailable",
        corporate_action_status="none",
        execution_metadata_version="factor-execution-evidence.v1",
    )


def _metadata(symbol: str) -> PaperInstrumentMetadata:
    market = "BJ" if symbol.endswith(".BJ") or symbol.startswith(("43", "83", "87", "88", "92")) else ("SH" if symbol.startswith(("5", "6", "9")) else "SZ")
    return PaperInstrumentMetadata(
        symbol=symbol,
        name="测试股票",
        market=market,
        list_date="2020-01-02",
        is_st=False,
        source="test-point-in-time-metadata",
        status_effective_date="2026-07-01",
    )


def _review_plan() -> AdviceReviewPlan:
    return AdviceReviewPlan(
        id=10,
        advice_id=20,
        symbol="600519",
        snapshot_market_time="2026-07-01 09:45:00",
        snapshot_price=100,
        snapshot_adjustment_mode="qfq",
        snapshot_anchor_date="2026-07-01",
        snapshot_anchor_close=100,
        snapshot_data_version="snapshot-v1",
        snapshot_contract_version=DAILY_KLINE_CONTRACT_VERSION,
        hypothesis="趋势延续",
        trigger_condition="次日开盘",
        invalidation_condition="跌破止损",
        target_price=110,
        stop_price=90,
        horizon_days=20,
        revision=1,
        plan_payload_digest="a" * 64,
        created_at="2026-07-01T00:00:00.000000Z",
        updated_at="2026-07-01T00:00:00.000000Z",
    )


def _strategy_create(plan: AdviceReviewPlan, *, allocation_pct: float) -> PaperStrategyCreate:
    return PaperStrategyCreate(
        plan_id=plan.id,
        expected_plan_revision=plan.revision,
        expected_plan_payload_digest=plan.plan_payload_digest,
        allocation_pct=allocation_pct,
    )


def _persist_review_plan_projection(path: Path, plan: AdviceReviewPlan) -> None:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT INTO advice_review_plan (
                id, advice_id, symbol, snapshot_market_time, snapshot_price,
                snapshot_adjustment_mode, snapshot_anchor_date, snapshot_anchor_close,
                snapshot_data_version, snapshot_contract_version, hypothesis,
                trigger_condition, invalidation_condition, target_price, stop_price,
                horizon_days, revision, plan_payload_digest, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.id,
                plan.advice_id,
                plan.symbol,
                plan.snapshot_market_time,
                plan.snapshot_price,
                plan.snapshot_adjustment_mode,
                plan.snapshot_anchor_date,
                plan.snapshot_anchor_close,
                plan.snapshot_data_version,
                plan.snapshot_contract_version,
                plan.hypothesis,
                plan.trigger_condition,
                plan.invalidation_condition,
                plan.target_price,
                plan.stop_price,
                plan.horizon_days,
                plan.revision,
                plan.plan_payload_digest,
                plan.created_at,
                plan.updated_at,
            ),
        )
        _backfill_current_plan_revisions(conn)
        digest = str(
            conn.execute(
                "SELECT plan_payload_digest FROM advice_review_plan WHERE id = ?",
                (plan.id,),
            ).fetchone()[0]
        )
    object.__setattr__(plan, "plan_payload_digest", digest)
