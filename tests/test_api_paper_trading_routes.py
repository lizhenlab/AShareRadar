from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_datahub
from app.api.routes import paper_trading
from app.models.market import DAILY_KLINE_CONTRACT_VERSION, Kline
from app.models.paper_trading import (
    PaperStrategy,
    PaperStrategyCreate,
    PaperTradingAccount,
    PaperTradingDashboard,
    PaperTradingPerformance,
)
from app.models.reviews import AdviceReviewPlan
from app.services.cache import SQLiteCache
from app.services.paper_trading import simulate_paper_portfolio
from app.services.paper_trading_costs import resolve_cost_profile
from tests.test_active_research_review_backend import _plan_input, _valid_analysis


def test_paper_dashboard_is_no_store_and_account_can_be_updated() -> None:
    cache = _PaperCache()
    client = _client(cache)

    response = client.get("/api/paper-trading")
    updated = client.patch("/api/paper-trading/account", json={"initial_cash": 2_000_000})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["performance"]["total_equity"] == 1_000_000
    assert updated.status_code == 200
    assert updated.json()["initial_cash"] == 2_000_000


def test_paper_strategy_route_freezes_plan_and_deletes_pending_strategy() -> None:
    cache = _PaperCache()
    client = _client(cache)

    created = client.post(
        "/api/paper-trading/strategies",
        json={
            "plan_id": 10,
            "expected_plan_revision": 1,
            "expected_plan_payload_digest": "a" * 64,
            "allocation_pct": 25,
        },
    )
    removed = client.delete("/api/paper-trading/strategies/7")

    assert created.status_code == 201
    assert created.json()["plan_id"] == 10
    assert created.json()["allocation_pct"] == 25
    assert cache.activation_market_time
    assert removed.json() == {"ok": True, "removed": True}
    assert cache.deleted == [7]


def test_paper_strategy_route_validates_allocation() -> None:
    response = _client(_PaperCache()).post(
        "/api/paper-trading/strategies",
        json={
            "plan_id": 10,
            "expected_plan_revision": 1,
            "expected_plan_payload_digest": "a" * 64,
            "allocation_pct": 101,
        },
    )

    assert response.status_code == 422


def test_paper_routes_publish_explicit_response_models() -> None:
    app = FastAPI()
    app.include_router(paper_trading.router)
    paths = app.openapi()["paths"]

    assert paths["/api/paper-trading"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PaperTradingDashboard"
    }
    assert paths["/api/paper-trading/run"]["post"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PaperSimulationSummary"
    }


def test_historical_run_compare_and_export_routes_use_immutable_snapshots(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "paper-api.sqlite3")
    advice = cache.save_advice_snapshot(
        _valid_analysis("600519"),
        snapshot_market_time="2026-07-01 09:45:00",
    )
    plan = cache.create_advice_review_plan(
        _plan_input(advice.id, "600519.SH").model_copy(
            update={"target_price": 110, "stop_price": 90, "horizon_days": 20}
        )
    )
    cache.create_paper_strategy(
        plan,
        PaperStrategyCreate(
            plan_id=plan.id,
            expected_plan_revision=plan.revision,
            expected_plan_payload_digest=plan.plan_payload_digest,
            allocation_pct=10,
        ),
        activation_market_time="2026-07-01 10:00:00",
    )
    strategy = cache.paper_strategies()[0]
    rows = {
        strategy.symbol: [
            _bar("2026-07-01", 100, 101, 99, 100),
            _bar("2026-07-02", 100, 105, 99, 104),
            _bar("2026-07-03", 104, 112, 103, 111),
        ]
    }
    first = simulate_paper_portfolio(
        cache.paper_trading_account(),
        [strategy],
        rows,
        as_of=datetime(2026, 7, 3, 16),
        cost_profile=resolve_cost_profile("base"),
    )
    second = simulate_paper_portfolio(
        cache.paper_trading_account(),
        [strategy],
        rows,
        as_of=datetime(2026, 7, 3, 16),
        cost_profile=resolve_cost_profile("stress"),
    )
    first_id = cache.save_paper_simulation(first).selected_run_id
    second_id = cache.save_paper_simulation(second).selected_run_id
    assert first_id and second_id
    client = _client(cache)

    runs = client.get("/api/paper-trading/runs")
    historical = client.get(f"/api/paper-trading/runs/{first_id}")
    compared = client.get(
        f"/api/paper-trading/runs/compare?left_run_id={first_id}&right_run_id={second_id}"
    )
    exported = client.get(f"/api/paper-trading/runs/{first_id}/export.json")
    csv_export = client.get(
        f"/api/paper-trading/runs/{first_id}/export.csv?dataset=events"
    )

    assert runs.status_code == 200
    assert [item["id"] for item in runs.json()] == [second_id, first_id]
    assert historical.status_code == 200
    assert historical.headers["cache-control"] == "no-store"
    assert historical.json()["selected_run_id"] == first_id
    assert compared.status_code == 200
    assert compared.json()["left_run"]["id"] == first_id
    assert compared.json()["right_run"]["id"] == second_id
    assert isinstance(compared.json()["deltas"]["total_cost"], float)
    assert compared.json()["left_run"]["cost_profile_id"] != compared.json()["right_run"]["cost_profile_id"]
    assert exported.status_code == 200
    assert exported.json()["run"]["input_fingerprint"] == first.input_fingerprint
    assert "attachment" in exported.headers["content-disposition"]
    assert csv_export.status_code == 200
    assert csv_export.content.startswith(b"\xef\xbb\xbfrun_id,sequence")
    assert "paper-trading-run-" in csv_export.headers["content-disposition"]


def _client(cache: object) -> TestClient:
    app = FastAPI()
    app.include_router(paper_trading.router)
    app.dependency_overrides[get_datahub] = lambda: _DataHubStub(cache)
    return TestClient(app)


@dataclass
class _DataHubStub:
    cache: object


class _PaperCache:
    def __init__(self) -> None:
        self.initial_cash = 1_000_000.0
        self.activation_market_time = ""
        self.deleted: list[int] = []

    def paper_trading_dashboard(self) -> PaperTradingDashboard:
        return _dashboard(self.initial_cash)

    def update_paper_trading_account(self, payload) -> PaperTradingAccount:
        self.initial_cash = payload.initial_cash
        return _account(self.initial_cash)

    def advice_review_plan(self, plan_id: int) -> AdviceReviewPlan | None:
        return _plan() if plan_id == 10 else None

    def create_paper_strategy(self, plan, payload, *, activation_market_time: str) -> PaperStrategy:
        self.activation_market_time = activation_market_time
        return _strategy(plan, payload.allocation_pct, activation_market_time)

    def delete_pending_paper_strategy(self, strategy_id: int) -> bool:
        self.deleted.append(strategy_id)
        return True


def _dashboard(initial_cash: float) -> PaperTradingDashboard:
    return PaperTradingDashboard(
        account=_account(initial_cash),
        performance=PaperTradingPerformance(
            strategy_count=0,
            pending_count=0,
            open_count=0,
            closed_count=0,
            skipped_count=0,
            data_unavailable_count=0,
            win_count=0,
            cash_balance=initial_cash,
            market_value=0,
            total_equity=initial_cash,
            realized_pnl=0,
            unrealized_pnl=0,
            total_return_pct=0,
            max_drawdown_pct=0,
        ),
    )


def _account(initial_cash: float) -> PaperTradingAccount:
    return PaperTradingAccount(
        name="本地模拟账户",
        initial_cash=initial_cash,
        modelled_one_way_friction_pct=0.05,
        created_at="2026-07-01T00:00:00.000000Z",
        updated_at="2026-07-01T00:00:00.000000Z",
    )


def _plan() -> AdviceReviewPlan:
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


def _bar(day: str, open_price: float, high: float, low: float, close: float) -> Kline:
    return Kline(
        date=day,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1_000,
        adjustment_mode="qfq",
        data_version="paper-api-test-v1",
        contract_version=DAILY_KLINE_CONTRACT_VERSION,
        as_of=f"{day} 15:15:00",
        point_in_time=True,
        session_status="trading",
        open_execution_status="tradable",
        corporate_action_status="none",
        execution_metadata_version="factor-execution-evidence.v1",
    )


def _strategy(plan: AdviceReviewPlan, allocation: float, activation: str) -> PaperStrategy:
    return PaperStrategy(
        id=7,
        plan_id=plan.id,
        plan_revision=plan.revision,
        plan_payload_digest=plan.plan_payload_digest,
        advice_id=plan.advice_id,
        symbol=plan.symbol,
        activation_market_time=activation,
        allocation_pct=allocation,
        snapshot_market_time=plan.snapshot_market_time,
        snapshot_price=plan.snapshot_price,
        snapshot_adjustment_mode=plan.snapshot_adjustment_mode,
        snapshot_anchor_date=plan.snapshot_anchor_date,
        snapshot_anchor_close=plan.snapshot_anchor_close,
        snapshot_data_version=plan.snapshot_data_version,
        snapshot_contract_version=plan.snapshot_contract_version,
        target_price=plan.target_price,
        stop_price=plan.stop_price,
        horizon_days=plan.horizon_days,
        status="pending",
        created_at="2026-07-01T00:00:00.000000Z",
        updated_at="2026-07-01T00:00:00.000000Z",
    )
