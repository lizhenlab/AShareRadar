from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.models.paper_trading import PaperTradingAccountUpdate
from app.services.cache import SQLiteCache
from app.services.paper_trading import simulate_paper_portfolio
from tests.test_paper_trading import _persist_review_plan_projection, _review_plan, _strategy_create


def test_empty_history_locks_cash_but_accepts_default_cost_only(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "paper-account.db")
    account = cache.paper_trading_account()
    draft = simulate_paper_portfolio(account, [], {}, as_of=datetime(2026, 7, 3, 16))
    saved = cache.save_paper_simulation(draft)
    assert saved.selected_run_id is not None
    assert saved.runs and not saved.strategies
    old_digest = saved.runs[0].output_digest

    changed = cache.update_paper_trading_account(PaperTradingAccountUpdate(default_cost_profile="stress"))
    assert changed.default_cost_profile == "stress"
    with pytest.raises(ValueError, match="已有模拟策略或运行"):
        cache.update_paper_trading_account(PaperTradingAccountUpdate(initial_cash=account.initial_cash))
    historical = cache.paper_trading_dashboard(run_id=saved.selected_run_id)
    assert historical.account.default_cost_profile == "stress"
    assert historical.runs[0].output_digest == old_digest
    assert historical.runs[0].cost_profile_id == saved.runs[0].cost_profile_id


def test_dashboard_rows_have_global_history_membership_not_a_status_permission(tmp_path: Path) -> None:
    path = tmp_path / "paper-membership.db"
    cache = SQLiteCache(path)
    account = cache.paper_trading_account()
    plan = _review_plan()
    _persist_review_plan_projection(path, plan)
    strategy = cache.create_paper_strategy(
        plan, _strategy_create(plan, allocation_pct=20), activation_market_time="2026-07-01 10:00:00",
    )
    before = cache.paper_trading_dashboard()
    assert before.selected_run_id is None and not before.runs
    assert [item.id for item in before.strategies] == [strategy.id]
    draft = simulate_paper_portfolio(account, [strategy], {}, as_of=datetime(2026, 7, 3, 16))
    saved = cache.save_paper_simulation(draft)
    assert saved.strategies[0].status == "data_unavailable"
    assert saved.strategies[0].allocation_order == 1
    with pytest.raises(ValueError, match="不可变历史运行"):
        cache.delete_pending_paper_strategy(strategy.id)

    later = plan.model_copy(update={"id": plan.id + 1, "advice_id": plan.advice_id + 1})
    _persist_review_plan_projection(path, later)
    pending = cache.create_paper_strategy(
        later, _strategy_create(later, allocation_pct=20), activation_market_time="2026-07-03 16:00:00",
    )
    historical = cache.paper_trading_dashboard(run_id=saved.selected_run_id)
    assert [item.id for item in historical.strategies] == [strategy.id]
    assert pending.id not in [item.id for item in historical.strategies]
    assert cache.delete_pending_paper_strategy(pending.id)
