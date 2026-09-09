from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from pathlib import Path
from typing import Iterator
import asyncio
import threading

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from fastapi import Response
import pytest

from app.api.errors import validation_exception_handler
from app.api.routes import discovery
from app.models.discovery import DiscoveryPresetCreate, DiscoveryPresetUpdate
from app.repositories.discovery import DiscoveryRepository
from app.repositories.market_scan_screen_alert import MarketScanScreenAlertRepository
from app.services.discovery import DiscoveryService
from app.services.market_scan_screen_alert import MarketScanScreenAlertService
from tests.test_market_scan_screen_alert import _missing, _preset, _published, _runtime, _seed, _write


def _history(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection, int]:
    cache, alerts = _runtime(tmp_path)
    preset = _preset(cache)
    seeds = [_seed(index) for index in range(1, 5)]
    _published(cache, seeds, [_write(1, 90), _write(2, 85), _write(3, 70), _write(4, 88)], data_date="2026-08-10")
    current = _published(cache, seeds, [_write(1, 70), _write(2, 86), _write(3, 92), _missing(4)], data_date="2026-08-11")
    alerts.record(preset_id=preset.id, current_run_id=current.id)
    service = DiscoveryService(DiscoveryRepository(cache.path), alerts)
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(discovery.router)
    app.dependency_overrides[discovery.get_discovery_service] = lambda: service
    conn = sqlite3.connect(cache.path)
    conn.row_factory = sqlite3.Row
    return TestClient(app), conn, preset.id


@pytest.fixture
def history(tmp_path: Path) -> Iterator[tuple[TestClient, sqlite3.Connection, int]]:
    client, conn, preset_id = _history(tmp_path)
    with client, conn:
        yield client, conn, preset_id
    conn.close()


def test_history_reads_stored_membership_without_present_day_names_or_writes(history) -> None:
    client, conn, preset_id = history
    base = f"/api/discovery/presets/{preset_id}/screen-alerts"
    before = list(conn.iterdump())
    listed = client.get(base)
    assert listed.status_code == 200
    summary = listed.json()["items"][0]
    assert set(summary) == {"id", "preset_id", "preset_revision", "current_run_id", "previous_run_id", "event_digest", "created_at", "entered_count", "exited_count", "suppressed_unrankable_count"}
    assert summary["entered_count"] == summary["exited_count"] == summary["suppressed_unrankable_count"] == 1
    details = client.get(f"{base}/{summary['id']}")
    assert details.status_code == 200
    assert details.json()["items"] == [{"symbol": "600003.SH", "change": "entered"}, {"symbol": "600001.SH", "change": "exited"}, {"symbol": "600004.SH", "change": "unrankable"}]
    assert details.json()["event"] == summary
    assert listed.headers["cache-control"] == details.headers["cache-control"] == "no-store"
    assert list(conn.iterdump()) == before
    repository = DiscoveryRepository(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
    repository.rename_preset(preset_id, name="现在的新名称", expected_revision=1, timestamp="2026-08-13 08:00:00")
    current = repository.preset(preset_id)
    repository.update_preset(preset_id, DiscoveryPresetUpdate(name="更新条件", criteria={"score": {"min": 99}}, expected_revision=current.revision), timestamp="2026-08-14 08:00:00")
    assert client.get(f"{base}/{summary['id']}").json() == details.json()


@pytest.mark.parametrize("kind,symbol", [("entered", "600003.SH"), ("exited", "600001.SH"), ("unrankable", "600004.SH")])
def test_detail_filters_and_pages_stored_membership(history, kind: str, symbol: str) -> None:
    client, _, preset_id = history
    base = f"/api/discovery/presets/{preset_id}/screen-alerts"
    event_id = client.get(base).json()["items"][0]["id"]
    page = client.get(f"{base}/{event_id}", params={"kind": kind, "page_size": 1}).json()
    assert page["total"] == page["page_count"] == 1
    assert page["items"] == [{"symbol": symbol, "change": kind}]
    beyond = client.get(f"{base}/{event_id}?kind={kind}&page=2&page_size=1").json()
    assert beyond["items"] == [] and beyond["total"] == 1
    all_page = client.get(f"{base}/{event_id}?page=2&page_size=1").json()
    assert all_page["items"] == [{"symbol": "600001.SH", "change": "exited"}]
    assert all_page["total"] == all_page["page_count"] == 3


@pytest.mark.parametrize("query", ["page=0", "page_size=0", "page_size=101", "kind=other"])
def test_history_rejects_invalid_pagination_and_kind(history, query: str) -> None:
    client, _, preset_id = history
    response = client.get(f"/api/discovery/presets/{preset_id}/screen-alerts/1?{query}")
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("corruption", ['{}', '[1]', '["600003"]', '["600003.sh"]', '[" 600003.SH"]', '["600003.SH","600003.SH"]', '["600001.SH"]'])
def test_bad_stored_membership_is_a_failure_not_an_empty_history(history, corruption: str) -> None:
    client, conn, preset_id = history
    conn.execute("UPDATE discovery_screen_alert_event SET entered_symbols_json = ?", (corruption,))
    conn.commit()
    base = f"/api/discovery/presets/{preset_id}/screen-alerts"
    for url in (base, f"{base}/1"):
        response = client.get(url)
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert corruption not in response.text


def test_history_sort_pagination_and_preset_boundaries(history) -> None:
    client, conn, preset_id = history
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    other = DiscoveryRepository(path).create_preset(DiscoveryPresetCreate(name="其他方案"), timestamp="2026-08-12 08:00:00")
    for digest in ("b" * 64, "c" * 64):
        conn.execute("""INSERT INTO discovery_screen_alert_event
            (preset_id,preset_revision,current_run_id,previous_run_id,event_digest,created_at,
             entered_symbols_json,exited_symbols_json,suppressed_unrankable_symbols_json)
            SELECT preset_id,preset_revision,current_run_id,previous_run_id,?,created_at,
             entered_symbols_json,exited_symbols_json,suppressed_unrankable_symbols_json
            FROM discovery_screen_alert_event WHERE id=1""", (digest,))
    conn.commit()
    base = f"/api/discovery/presets/{preset_id}/screen-alerts"
    pages = [client.get(f"{base}?page={page}&page_size=1").json() for page in range(1, 5)]
    assert [page["items"][0]["id"] for page in pages[:3]] == [3, 2, 1]
    assert all(page["total"] == page["page_count"] == 3 for page in pages)
    assert pages[-1]["items"] == []
    empty = client.get(f"/api/discovery/presets/{other.id}/screen-alerts").json()
    assert empty["items"] == [] and empty["total"] == empty["page_count"] == 0
    for url in (f"{base}/9999", f"/api/discovery/presets/{other.id}/screen-alerts/1", "/api/discovery/presets/9999/screen-alerts", "/api/discovery/presets/9999/screen-alerts/1"):
        response = client.get(url)
        assert response.status_code == 404 and response.headers["cache-control"] == "no-store"


def test_history_uses_query_only_snapshot_and_never_compiles_current_preset(history, monkeypatch) -> None:
    _, conn, preset_id = history
    repository = MarketScanScreenAlertRepository(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
    original = repository._read_snapshot
    statements: list[str] = []

    @contextmanager
    def observed_snapshot():
        with original() as snapshot:
            assert snapshot.execute("PRAGMA query_only").fetchone()[0] == 1
            snapshot.set_trace_callback(statements.append)
            yield snapshot

    monkeypatch.setattr(repository, "_read_snapshot", observed_snapshot)
    service = MarketScanScreenAlertService(repository)
    conn.execute("UPDATE discovery_preset SET criteria_json = '{}' WHERE id = ?", (preset_id,))
    conn.commit()
    assert service.history(preset_id, page=1, page_size=20).total == 1
    assert service.detail(preset_id, 1, page=1, page_size=20, kind="all").total == 3
    assert all(statement.lstrip().upper().startswith(("SELECT", "COMMIT", "ROLLBACK")) for statement in statements)
    assert not any("market_scan_result" in statement or "market_scan_run" in statement for statement in statements)


@pytest.mark.parametrize("column,value", [("created_at", "broken"), ("event_digest", "x" * 64), ("previous_run_id", 2), ("entered_symbols_json", "[invalid")])
def test_malformed_stored_metadata_fails_closed(history, column: str, value: object) -> None:
    client, conn, preset_id = history
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(f"UPDATE discovery_screen_alert_event SET {column} = ?", (value,))
    conn.commit()
    response = client.get(f"/api/discovery/presets/{preset_id}/screen-alerts")
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("page,page_size", [(0, 1), (True, 1), (1, 0), (1, 101), (10**100, 100)])
def test_repository_rejects_invalid_pagination_without_sql(history, page: int, page_size: int) -> None:
    _, conn, preset_id = history
    repository = MarketScanScreenAlertRepository(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
    with pytest.raises(ValueError):
        repository.event_history(preset_id, page=page, page_size=page_size)


def test_list_count_and_rows_share_one_read_snapshot(history, monkeypatch) -> None:
    _, conn, preset_id = history
    repository = MarketScanScreenAlertRepository(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
    original = repository._read_snapshot
    changed = False

    def change_between_queries(statement: str) -> None:
        nonlocal changed
        if not changed and "COUNT(*)" in statement:
            changed = True
            conn.execute("DELETE FROM discovery_screen_alert_event")
            conn.commit()

    @contextmanager
    def snapshot_with_concurrent_delete():
        with original() as snapshot:
            snapshot.set_trace_callback(change_between_queries)
            yield snapshot

    monkeypatch.setattr(repository, "_read_snapshot", snapshot_with_concurrent_delete)
    page = repository.event_history(preset_id, page=1, page_size=20)
    assert changed and page.total == len(page.items) == 1
    assert repository.event_history(preset_id, page=1, page_size=20).total == 0


def test_history_route_offloads_sql_read_from_event_loop(history, monkeypatch) -> None:
    _, conn, preset_id = history
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    repository = MarketScanScreenAlertRepository(path)
    service = DiscoveryService(DiscoveryRepository(path), MarketScanScreenAlertService(repository))
    original = repository.event_history
    worker_threads: list[int] = []

    def observed_read(preset_id: int, *, page: int, page_size: int):
        worker_threads.append(threading.get_ident())
        return original(preset_id, page=page, page_size=page_size)

    monkeypatch.setattr(repository, "event_history", observed_read)

    async def read_from_loop() -> None:
        loop_thread = threading.get_ident()
        result = await discovery.discovery_screen_alert_history(Response(), preset_id, 1, 20, service)
        assert result.total == 1
        assert worker_threads and worker_threads[0] != loop_thread

    asyncio.run(read_from_loop())


def test_only_the_requested_detail_page_materializes_item_models(history, monkeypatch) -> None:
    from app.repositories import market_scan_screen_alert as repository_module

    _, conn, preset_id = history
    repository = MarketScanScreenAlertRepository(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
    original = repository_module.MarketScanScreenAlertHistoryItem
    materialized: list[str] = []

    def record_item(**values):
        materialized.append(values["symbol"])
        return original(**values)

    monkeypatch.setattr(repository_module, "MarketScanScreenAlertHistoryItem", record_item)
    assert repository.event_history(preset_id, page=1, page_size=20).total == 1
    assert materialized == []
    result = repository.event_detail(preset_id, 1, page=2, page_size=1, kind="all")
    assert result.total == 3
    assert materialized == ["600001.SH"]
