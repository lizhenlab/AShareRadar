from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import sqlite3
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from app.api.routes.discovery import get_discovery_service
from app.config import Settings
from app.repositories.discovery import DiscoveryRepository
from app.services.discovery import DiscoveryService
from tests.test_discovery_presets import _preset_payload, _service


@pytest.mark.parametrize("mutation", ["insert", "delete"])
@pytest.mark.parametrize("pagination", [(1, 20), (2, 1)])
def test_preset_page_and_total_share_one_snapshot_during_concurrent_mutation(tmp_path, monkeypatch, mutation, pagination):
    monkeypatch.setattr("app.config_settings._SHELL_ENV_VALUES", {})
    from app.main import create_app

    path, reader = _service(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    first = reader.create_preset(_preset_payload("First preset"))
    second = reader.create_preset(_preset_payload("Second preset"))
    writer = DiscoveryService(DiscoveryRepository(path))
    application = create_app(
        settings=Settings(
            cache_path=tmp_path / "unused.sqlite3", scheduler_enabled=False, llm_enabled=False,
            cors_allow_origins=("http://testserver",),
        ),
        container_factory=lambda: pytest.fail("read-only API regression must not start the application runtime"),
    )
    application.dependency_overrides[get_discovery_service] = lambda: reader
    client = TestClient(application, raise_server_exceptions=False)
    page, page_size = pagination
    endpoint = f"/api/discovery/presets?page={page}&page_size={page_size}"
    before = client.get(endpoint)
    assert before.status_code == 200
    between_queries, committed = Event(), Event()
    hook_timeouts = []
    connect = reader.repository._connections.connect

    @contextmanager
    def observed_connection():
        with connect() as conn:
            def trace(statement):
                if "SELECT * FROM discovery_preset" in statement and "ORDER BY" in statement:
                    between_queries.set()
                    if not committed.wait(5):
                        hook_timeouts.append("writer did not finish while the read snapshot was open")
            conn.set_trace_callback(trace)
            yield conn

    def mutate():
        assert between_queries.wait(5)
        try:
            if mutation == "insert":
                return writer.create_preset(_preset_payload("Concurrent preset"))
            writer.delete_preset(first.id, expected_revision=first.revision)
            return None
        finally:
            committed.set()

    try:
        with patch.object(reader.repository._connections, "connect", side_effect=observed_connection):
            with ThreadPoolExecutor(max_workers=1) as workers:
                pending = workers.submit(mutate)
                response = client.get(endpoint)
                created = pending.result(timeout=5)
        assert not hook_timeouts
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == before.json()
        refreshed = client.get(endpoint)
        assert refreshed.status_code == 200
        assert refreshed.json()["total"] == (3 if mutation == "insert" else 1)
        complete = client.get("/api/discovery/presets?page=1&page_size=20")
        assert complete.status_code == 200
        expected = {first.id, second.id, created.id} if created is not None else {second.id}
        assert {item["id"] for item in complete.json()["items"]} == expected
        assert complete.json()["total"] == len(expected)
    finally:
        client.close()
