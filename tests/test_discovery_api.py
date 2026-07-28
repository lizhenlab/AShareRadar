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
    renamed = client.patch(
        f"/api/discovery/presets/{preset_id}",
        json={"name": "重命名方案", "expected_revision": 1},
    )
    exported = client.get(f"/api/discovery/presets/{preset_id}/export")
    deleted = client.delete(
        f"/api/discovery/presets/{preset_id}",
        params={"expected_revision": 2},
    )

    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert renamed.status_code == 200
    assert renamed.json()["revision"] == 2
    assert exported.status_code == 200
    assert exported.json()["schema_version"] == 1
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "preset_id": preset_id}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/discovery/presets?page=0", None),
        ("get", "/api/discovery/presets?page_size=101", None),
        ("delete", "/api/discovery/presets/1?expected_revision=0", None),
        ("post", "/api/discovery/presets", {**_payload(), "criteria": {"keyword": "茅台"}}),
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
    assert "/api/discovery/runs/{run_id}/rank-changes" in paths


def test_route_reuses_cache_owned_discovery_service_and_shared_lock(tmp_path) -> None:
    cache = SQLiteCache(tmp_path / "shared.sqlite3")
    hub = _CacheHub(cache)

    first = discovery.get_discovery_service(hub)
    second = discovery.get_discovery_service(hub)

    assert first is cache.discovery_service
    assert second is first
    assert cache.discovery_service.repository is cache.discovery_repo
    assert cache.discovery_repo._lock is cache._lock


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


class _CacheHub:
    def __init__(self, cache: SQLiteCache) -> None:
        self.cache = cache
