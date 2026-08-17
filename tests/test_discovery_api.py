from __future__ import annotations

import sqlite3
import threading

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
import pytest

from app.api.errors import validation_exception_handler
from app.api.routes import discovery
from app.db.schema import initialize_schema
from app.repositories.discovery import DiscoveryRepository
from app.models.market_scan_screen_alert import (
    MarketScanScreenAlertPresetRef,
    MarketScanScreenAlertResponse,
    MarketScanScreenAlertRunRef,
)
from app.services.cache import SQLiteCache
from app.services.discovery import DiscoveryService


def _payload() -> dict[str, object]:
    return {
        "name": "API 方案",
        "criteria": {
            "market": ["SH"],
            "quality": {"min": 70},
            "score": {"min": 80},
        },
        "sort": [{"field": "score", "order": "desc"}],
    }


def test_discovery_preset_routes_expose_typed_crud_and_export_contract(tmp_path) -> None:
    client = _client(tmp_path)
    created = client.post("/api/discovery/presets", json=_payload())

    assert created.status_code == 201
    preset_id = created.json()["id"]
    assert created.json()["revision"] == 1

    listed = client.get("/api/discovery/presets", params={"page": 1, "page_size": 10})
    updated = client.put(
        f"/api/discovery/presets/{preset_id}",
        json={
            **_payload(),
            "name": "更新方案",
            "criteria": {"market": ["SH", "SZ"], "score": {"min": 85}},
            "sort": [
                {"field": "score", "order": "desc"},
                {"field": "symbol", "order": "asc"},
            ],
            "expected_revision": 1,
        },
    )
    renamed = client.patch(
        f"/api/discovery/presets/{preset_id}",
        json={"name": "重命名方案", "expected_revision": 2},
    )
    exported = client.get(f"/api/discovery/presets/{preset_id}/export")
    deleted = client.delete(
        f"/api/discovery/presets/{preset_id}",
        params={"expected_revision": 3},
    )

    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["criteria"]["market"] == ["SH", "SZ"]
    assert renamed.status_code == 200
    assert renamed.json()["revision"] == 3
    assert exported.status_code == 200
    assert exported.json()["schema_version"] == 2
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "preset_id": preset_id}
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in (created, listed, updated, renamed, exported, deleted)
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/discovery/presets?page=0", None),
        ("get", "/api/discovery/presets?page_size=101", None),
        ("delete", "/api/discovery/presets/1?expected_revision=0", None),
        ("post", "/api/discovery/presets", {**_payload(), "criteria": {"keyword": "茅台\n非法"}}),
        ("post", "/api/discovery/presets", {**_payload(), "criteria": {"is_new": "false"}}),
        ("post", "/api/discovery/presets", {**_payload(), "unknown": True}),
        (
            "post",
            "/api/discovery/presets/1/apply",
            {"run_id": 1, "page": 0, "page_size": 50},
        ),
        (
            "post",
            "/api/discovery/presets/1/research-queue",
            {"run_id": 1, "expected_preset_revision": 1, "symbols": []},
        ),
        (
            "post",
            "/api/discovery/presets/1/screen-alerts",
            {"current_run_id": 0, "expected_preset_revision": 1},
        ),
        ("get", "/api/discovery/runs/1/rank-changes?page_size=201", None),
    ],
)
def test_discovery_routes_reject_invalid_fields_and_pagination(
    tmp_path,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    response = _client(tmp_path).request(method, path, json=body)

    assert response.status_code == 422


def test_every_discovery_route_declares_a_response_model() -> None:
    assert discovery.router.routes
    assert all(route.response_model is not None for route in discovery.router.routes)


def test_main_app_registers_discovery_routes() -> None:
    from app.main import create_app

    paths = {route.path for route in create_app().routes}

    assert "/api/discovery/presets" in paths
    assert "/api/discovery/presets/{preset_id}/apply" in paths
    assert "/api/discovery/presets/{preset_id}/screen-alerts" in paths
    assert "/api/discovery/runs/{run_id}/rank-changes" in paths


def test_route_reuses_composed_discovery_service_and_shared_lock(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "shared.sqlite3")
    services = cache.domain_services

    first = discovery.get_discovery_service(services)
    second = discovery.get_discovery_service(services)

    assert first is cache.discovery_service
    assert second is first
    assert cache.discovery_service.repository is cache.discovery_repo
    assert cache.discovery_repo._lock is cache._lock


def test_discovery_screen_alert_route_exposes_typed_idempotent_contract(tmp_path) -> None:
    digest = "a" * 64
    service = _ScreenAlertRouteService(digest)
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(discovery.router)
    app.dependency_overrides[discovery.get_discovery_service] = lambda: service

    first = TestClient(app).post(
        "/api/discovery/presets/7/screen-alerts",
        json={"current_run_id": 42, "expected_preset_revision": 3},
    )

    assert first.status_code == 200
    assert first.json() == service.response.model_dump(mode="json")
    assert service.called_with == (7, 42, 3)


def test_exclusive_local_data_operation_blocks_discovery_database_access(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "exclusive.sqlite3")
    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def read_presets() -> None:
        started.set()
        try:
            cache.discovery_service.list_presets(page=1, page_size=20)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    with cache.exclusive_local_data_operation():
        thread = threading.Thread(target=read_presets)
        thread.start()
        assert started.wait(timeout=1)
        assert finished.wait(timeout=0.05) is False

    assert finished.wait(timeout=2)
    thread.join(timeout=1)
    assert errors == []


def _client(tmp_path) -> TestClient:
    path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(path) as conn:
        initialize_schema(conn)
    service = DiscoveryService(DiscoveryRepository(path))
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(discovery.router)
    app.dependency_overrides[discovery.get_discovery_service] = lambda: service
    return TestClient(app)


class _ScreenAlertRouteService:
    def __init__(self, digest: str) -> None:
        run = {
            "status": "success",
            "mode": "official",
            "scope": "SH/SZ/BJ listed A-shares",
            "rule_version": "full-market-score-v4",
            "data_date": "2026-08-11",
            "finished_at": "2026-08-11T15:30:00+08:00",
        }
        self.response = MarketScanScreenAlertResponse(
            status="ready",
            preset=MarketScanScreenAlertPresetRef(
                preset_id=7,
                preset_revision=3,
                preset_name="高质量",
                spec_digest=digest,
            ),
            current=MarketScanScreenAlertRunRef(run_id=42, **run),
            previous=MarketScanScreenAlertRunRef(run_id=41, **run),
            entered_symbols=("600519.SH",),
            event_digest=digest,
            created=True,
        )
        self.called_with: tuple[int, int, int | None] | None = None

    def record_screen_alert(self, preset_id, request):
        self.called_with = (
            preset_id,
            request.current_run_id,
            request.expected_preset_revision,
        )
        return self.response
