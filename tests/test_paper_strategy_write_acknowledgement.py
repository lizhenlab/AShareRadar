"""Paper strategy creation must verify its return model before committing."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from app.repositories import paper_trading
from app.services.cache import SQLiteCache
from app.services.paper_trading import create_paper_strategy
from tests.test_active_research_review_backend import _plan_input, _valid_analysis
from tests.test_api_paper_trading_routes import _client
from tests.test_paper_trading import _strategy_create


@pytest.mark.parametrize("fault", ["mapping", "commit"])
def test_failed_strategy_receipt_rolls_back_and_same_plan_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    path = tmp_path / "paper-strategy-receipt.db"
    cache = SQLiteCache(path)
    plan = _saved_review_plan(cache)
    payload = _strategy_create(plan, allocation_pct=20)
    connections = []
    original_connect = cache.paper_trading_repo._connect

    @contextmanager
    def inspect_connection():
        with original_connect() as connection:
            connections.append(connection)
            if fault == "commit":
                connection.set_authorizer(lambda action, argument, *_: (
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT"
                    else sqlite3.SQLITE_OK
                ))
            yield connection

    with monkeypatch.context() as scoped:
        scoped.setattr(cache.paper_trading_repo, "_connect", inspect_connection)
        if fault == "mapping":
            scoped.setattr(paper_trading, "_strategy_from_row", _fail_strategy_model)
        error_type = RuntimeError if fault == "mapping" else sqlite3.DatabaseError
        error_message = "acknowledgement model failure" if fault == "mapping" else "not authorized"
        with pytest.raises(error_type, match=error_message):
            create_paper_strategy(cache, payload, now=datetime(2026, 7, 20, 10))
    assert cache.paper_strategies() == [], "failed acknowledgement committed a strategy"
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")

    saved = create_paper_strategy(cache, payload, now=datetime(2026, 7, 20, 10))
    assert saved.plan_id == plan.id and saved.plan_revision == plan.revision
    assert saved.plan_payload_digest == plan.plan_payload_digest
    assert saved.allocation_pct == 20
    assert saved.target_price == plan.target_price and saved.stop_price == plan.stop_price
    assert SQLiteCache(path).paper_strategies() == [saved]
    with pytest.raises(ValueError, match="已加入模拟交易"):
        create_paper_strategy(cache, payload, now=datetime(2026, 7, 20, 10))
    assert cache.paper_strategies() == [saved]


def test_strategy_api_failed_receipt_does_not_lock_out_a_successful_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "paper-strategy-api.db"
    cache = SQLiteCache(path)
    plan = _saved_review_plan(cache)
    payload = _strategy_create(plan, allocation_pct=20).model_dump(mode="json")
    client = _client(cache)
    with monkeypatch.context() as scoped:
        scoped.setattr(paper_trading, "_strategy_from_row", _fail_strategy_model)
        failed = client.post("/api/paper-trading/strategies", json=payload)
    assert failed.status_code == 503
    assert cache.paper_strategies() == []
    retried = client.post("/api/paper-trading/strategies", json=payload)
    assert retried.status_code == 201
    assert retried.json() == cache.paper_strategies()[0].model_dump(mode="json")
    assert len(cache.paper_strategies()) == 1


def _fail_strategy_model(_row):
    raise RuntimeError("simulated paper strategy acknowledgement model failure")


def _saved_review_plan(cache):
    advice = cache.save_advice_snapshot(
        _valid_analysis("600519"), snapshot_market_time="2026-07-17 15:15:00",
    )
    return cache.create_advice_review_plan(_plan_input(advice.id, "600519.SH"))
