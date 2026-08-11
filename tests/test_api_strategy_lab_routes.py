from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.api.errors import validation_exception_handler
from app.api.routes import strategy_lab
from app.db.schema import initialize_schema
from app.repositories.strategy_lab import StrategyLabRepository
from app.services.strategy_lab import StrategyLabService


def test_strategy_lab_routes_parse_compile_save_version_diff_copy_and_archive(tmp_path) -> None:
    client = _client(tmp_path)
    parsed = client.post(
        "/api/strategy-lab/parse",
        json={
            "text": "排除ST，选择沪深A股中成交额超过1亿的股票，行业最多3只，持有5天",
            "name": "中文策略",
        },
    )
    assert parsed.status_code == 200
    parsed_json = parsed.json()
    assert parsed_json["requires_confirmation"] is True
    assert parsed_json["draft"]["hard_filters"][0]["field"] == "amount"

    compiled = client.post(
        "/api/strategy-lab/compile",
        json={"spec": parsed_json["draft"], "dry_run": True},
    )
    assert compiled.status_code == 200
    assert compiled.json()["execution_plan"]["will_start_scan"] is False

    created = client.post(
        "/api/strategy-lab/strategies",
        json={"spec": parsed_json["draft"], "confirmed": True},
    )
    assert created.status_code == 201
    strategy_id = created.json()["strategy_id"]
    original_fingerprint = created.json()["fingerprint"]

    updated_spec = created.json()["spec"]
    updated_spec["portfolio_constraints"]["stock_count"] = 10
    updated_spec["portfolio_constraints"]["max_stock_weight"] = 0.1
    updated = client.put(
        f"/api/strategy-lab/strategies/{strategy_id}",
        json={"spec": updated_spec, "expected_revision": 1, "confirmed": True},
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["fingerprint"] != original_fingerprint

    historical = client.get(
        f"/api/strategy-lab/strategies/{strategy_id}",
        params={"revision": 1},
    )
    versions = client.get(f"/api/strategy-lab/strategies/{strategy_id}/versions")
    diff = client.get(
        f"/api/strategy-lab/strategies/{strategy_id}/diff",
        params={"left_revision": 1, "right_revision": 2},
    )
    copied = client.post(
        f"/api/strategy-lab/strategies/{strategy_id}/copy",
        json={"name": "中文策略副本", "revision": 1, "confirmed": True},
    )
    archived = client.post(
        f"/api/strategy-lab/strategies/{strategy_id}/archive",
        json={"expected_revision": 2, "archived": True},
    )

    assert historical.status_code == 200
    assert historical.json()["fingerprint"] == original_fingerprint
    assert historical.json()["current_revision"] == 2
    assert versions.json()["total"] == 2
    assert "portfolio_constraints.stock_count" in diff.json()["changed_paths"]
    assert copied.status_code == 201
    assert copied.json()["fingerprint"] == original_fingerprint
    assert archived.json()["archived"] is True

    active = client.get("/api/strategy-lab/strategies")
    all_rows = client.get(
        "/api/strategy-lab/strategies",
        params={"include_archived": True},
    )
    assert active.json()["total"] == 1
    assert all_rows.json()["total"] == 2


def test_strategy_lab_routes_reject_unconfirmed_unknown_and_unsafe_inputs(tmp_path) -> None:
    client = _client(tmp_path)
    unconfirmed = client.post(
        "/api/strategy-lab/strategies",
        json={"spec": {"name": "未确认"}, "confirmed": False},
    )
    unsafe = client.post(
        "/api/strategy-lab/compile",
        json={
            "spec": {
                "name": "注入",
                "hard_filters": [
                    {"field": "amount;DROP_TABLE", "operator": "gte", "value": 1}
                ],
            }
        },
    )
    unknown = client.post(
        "/api/strategy-lab/strategies",
        json={
            "spec": {
                "name": "未知字段",
                "hard_filters": [
                    {"field": "unknown_metric", "operator": "gte", "value": 1}
                ],
            },
            "confirmed": True,
        },
    )

    assert unconfirmed.status_code == 422
    assert unsafe.status_code == 422
    assert unknown.status_code == 400
    assert "尚不可执行" in unknown.json()["detail"]


def test_every_strategy_lab_route_has_strict_response_model_and_main_registration() -> None:
    from app.main import create_app

    assert strategy_lab.router.routes
    assert all(route.response_model is not None for route in strategy_lab.router.routes)
    paths = {route.path for route in create_app().routes}
    assert "/api/strategy-lab/parse" in paths
    assert "/api/strategy-lab/strategies" in paths
    assert "/api/strategy-lab/strategies/{strategy_id}/diff" in paths


def _client(tmp_path) -> TestClient:
    path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(path) as conn:
        initialize_schema(conn)
    service = StrategyLabService(StrategyLabRepository(path))
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(strategy_lab.router)
    app.dependency_overrides[strategy_lab.get_strategy_lab_service] = lambda: service
    return TestClient(app)
