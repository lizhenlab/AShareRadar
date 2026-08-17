from __future__ import annotations

import asyncio
import sqlite3

from fastapi import FastAPI, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
import pytest

from app.api.errors import validation_exception_handler
from app.api.routes import strategy_lab
from app.db.schema import initialize_schema
from app.repositories.strategy_lab import StrategyLabRepository
from app.repositories.strategy_automation import StrategyAutomationIntegrityError
from app.repositories.strategy_evidence import StrategyEvidenceIntegrityError
from app.repositories.strategy_execution import StrategyExecutionIntegrityError
from app.models.strategy_execution import StrategyExecutionRequest
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
    assert "/api/strategy-lab/templates" in paths
    assert "/api/strategy-lab/executable-candidate-shadow" in paths
    assert "/api/strategy-lab/strategies" in paths
    assert "/api/strategy-lab/strategies/{strategy_id}/diff" in paths


def test_strategy_template_catalog_route_is_read_only_and_not_cached(tmp_path) -> None:
    response = _client(tmp_path).get("/api/strategy-lab/templates")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["schema_version"] == "full-market-strategy-template-catalog-v1"
    assert payload["production_effect"] == "none"
    assert payload["official_session_count"] == 2
    assert len(payload["catalog_digest"]) == 64
    assert len(payload["templates"]) == 14


def test_strategy_execution_success_sets_no_store_before_service_call() -> None:
    sentinel = object()

    class ExecutionService:
        def execute(self, _payload: StrategyExecutionRequest) -> object:
            return sentinel

    response = Response()
    result = asyncio.run(
        strategy_lab.execute_strategy(
            response,
            StrategyExecutionRequest(strategy_id=1),
            ExecutionService(),  # type: ignore[arg-type]
        )
    )

    assert result is sentinel
    assert response.headers["cache-control"] == "no-store"


def test_strategy_execution_integrity_error_is_generic_conflict() -> None:
    class ExecutionService:
        def execute(self, _payload: StrategyExecutionRequest) -> None:
            raise StrategyExecutionIntegrityError("候选摘要损坏 /private/ledger.sqlite3")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            strategy_lab.execute_strategy(
                Response(),
                StrategyExecutionRequest(strategy_id=1),
                ExecutionService(),  # type: ignore[arg-type]
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "策略执行账本完整性校验失败，已拒绝读取"
    assert "private" not in str(exc_info.value.detail)


def test_strategy_evidence_and_simulation_integrity_errors_are_generic_conflicts() -> None:
    class CorruptEvidenceService:
        def latest(self, *_args: object, **_kwargs: object) -> None:
            raise StrategyEvidenceIntegrityError("sensitive evidence row and digest")

        def refresh(self, *_args: object, **_kwargs: object) -> None:
            raise StrategyEvidenceIntegrityError("sensitive evidence artifact path")

    class CorruptAutomationService:
        def create_simulation_plan(self, *_args: object, **_kwargs: object) -> None:
            raise StrategyAutomationIntegrityError("sensitive execution seal")

        def simulation_plan(self, *_args: object, **_kwargs: object) -> None:
            raise StrategyAutomationIntegrityError("sensitive stored plan")

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(strategy_lab.router)
    app.dependency_overrides[strategy_lab.get_strategy_evidence_service] = CorruptEvidenceService
    app.dependency_overrides[strategy_lab.get_strategy_automation_service] = CorruptAutomationService
    client = TestClient(app)

    responses = (
        client.get("/api/strategy-lab/strategies/1/evidence"),
        client.post("/api/strategy-lab/strategies/1/evidence/refresh", json={}),
        client.post("/api/strategy-lab/executions/1/simulation-plan"),
        client.get("/api/strategy-lab/executions/1/simulation-plan"),
    )
    assert {response.status_code for response in responses} == {409}
    assert {
        response.json()["detail"] for response in responses
    } == {"研究 artifact 完整性校验失败，已拒绝读取"}
    assert all("sensitive" not in response.text for response in responses)


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
