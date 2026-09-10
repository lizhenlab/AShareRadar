"""Cash-change admission and its acknowledgement share one SQLite transaction."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3
import threading

import pytest

from app.models.paper_trading import PaperTradingAccountUpdate
from app.repositories import paper_trading
from app.services.cache import SQLiteCache
from app.services.paper_trading import simulate_paper_portfolio


def test_first_run_committing_before_account_admission_prevents_cash_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "history-first.db"
    account_cache, history_cache = SQLiteCache(path), SQLiteCache(path)
    account = account_cache.paper_trading_account()
    draft = simulate_paper_portfolio(account, [], {}, as_of=datetime(2026, 7, 3, 16))
    history_written, release_history, account_attempting_write = (threading.Event() for _ in range(3))
    history_connect = history_cache.paper_trading_repo._connect
    account_connect = account_cache.paper_trading_repo._connect

    @contextmanager
    def hold_history_commit():
        with history_connect() as connection:
            yield connection
            history_written.set()
            assert release_history.wait(10), "account admission was never attempted"

    @contextmanager
    def observe_account_admission():
        with account_connect() as connection:
            connection.set_trace_callback(
                lambda sql: account_attempting_write.set() if sql.startswith("BEGIN") else None
            )
            yield connection

    monkeypatch.setattr(history_cache.paper_trading_repo, "_connect", hold_history_commit)
    monkeypatch.setattr(account_cache.paper_trading_repo, "_connect", observe_account_admission)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = pool.submit(history_cache.save_paper_simulation, draft)
        try:
            assert history_written.wait(10), "first history did not reach its uncommitted write"
            changed = pool.submit(
                account_cache.update_paper_trading_account,
                PaperTradingAccountUpdate(initial_cash=2_000_000),
            )
            assert account_attempting_write.wait(10), "account update did not attempt its write lock"
        finally:
            release_history.set()
        assert saved.result(timeout=10).selected_run_id is not None
        with pytest.raises(ValueError, match="已有模拟策略或运行"):
            changed.result(timeout=10)
    assert account_cache.paper_trading_account().initial_cash == account.initial_cash
    assert len(history_cache.paper_trading_runs()) == 1


def test_account_admission_reserves_write_lock_but_preserves_prepared_simulation_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "account-first.db"
    account_cache, history_cache = SQLiteCache(path), SQLiteCache(path)
    prepared = simulate_paper_portfolio(
        account_cache.paper_trading_account(), [], {}, as_of=datetime(2026, 7, 3, 16),
    )
    original_account_connect = account_cache.paper_trading_repo._connect
    original_history_connect = history_cache.paper_trading_repo._connect
    attempts = []

    @contextmanager
    def immediate_history_attempt():
        with original_history_connect() as connection:
            connection.execute("PRAGMA busy_timeout = 0")
            yield connection

    @contextmanager
    def attempt_history_before_account_read():
        with original_account_connect() as connection:
            def trace(sql):
                if not sql.startswith("SELECT * FROM paper_trading_account") or attempts:
                    return
                attempts.append("attempting")
                try:
                    history_cache.save_paper_simulation(prepared)
                except sqlite3.OperationalError as error:
                    attempts.append(str(error))
                else:
                    attempts.append("committed before account admission")

            connection.set_trace_callback(trace)
            yield connection

    with monkeypatch.context() as scoped:
        scoped.setattr(history_cache.paper_trading_repo, "_connect", immediate_history_attempt)
        scoped.setattr(account_cache.paper_trading_repo, "_connect", attempt_history_before_account_read)
        changed = account_cache.update_paper_trading_account(PaperTradingAccountUpdate(initial_cash=2_000_000))
    assert attempts == ["attempting", "database is locked"]
    assert changed.initial_cash == 2_000_000
    assert history_cache.paper_trading_runs() == []
    saved = history_cache.save_paper_simulation(prepared)
    assert saved.account.initial_cash == 2_000_000
    assert saved.runs[0].configuration["initial_cash"] == 1_000_000
    assert saved.performance.total_equity == 1_000_000


@pytest.mark.parametrize("fault", ["mapping", "commit"])
def test_failed_account_acknowledgement_rolls_back_and_releases_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    cache = SQLiteCache(tmp_path / "account-failure.db")
    before = cache.paper_trading_account()
    connections = []
    original_connect = cache.paper_trading_repo._connect

    @contextmanager
    def failing_connect():
        with original_connect() as connection:
            connections.append(connection)
            if fault == "commit":
                connection.set_authorizer(lambda action, argument, *_: (
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT"
                    else sqlite3.SQLITE_OK
                ))
            yield connection

    def fail_mapping(_row):
        raise RuntimeError("account acknowledgement model construction failed")

    update = PaperTradingAccountUpdate(initial_cash=2_000_000, default_cost_profile="stress")
    with monkeypatch.context() as scoped:
        scoped.setattr(cache.paper_trading_repo, "_connect", failing_connect)
        if fault == "mapping":
            scoped.setattr(paper_trading, "_account_from_row", fail_mapping)
        with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
            cache.update_paper_trading_account(update)
    assert cache.paper_trading_account() == before
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    changed = cache.update_paper_trading_account(update)
    assert changed.initial_cash == 2_000_000 and changed.default_cost_profile == "stress"
    assert SQLiteCache(cache.path).paper_trading_account() == changed


def test_cost_only_update_remains_allowed_after_history_without_changing_frozen_run(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "cost-only.db")
    original = cache.paper_trading_account()
    draft = simulate_paper_portfolio(original, [], {}, as_of=datetime(2026, 7, 3, 16))
    saved = cache.save_paper_simulation(draft)
    changed = cache.update_paper_trading_account(PaperTradingAccountUpdate(default_cost_profile="stress"))
    assert changed.initial_cash == original.initial_cash
    assert changed.default_cost_profile == "stress"
    assert cache.paper_trading_runs()[0] == saved.runs[0]
    assert cache.paper_trading_account() == changed
