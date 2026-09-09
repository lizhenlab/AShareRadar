"""Exact discovery source retries preserve subsequent user-managed queue state."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import sqlite3
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_datahub, get_domain_services
from app.api.routes import discovery, watchlist
from app.config import Settings
from app.models.discovery import DiscoveryResearchQueueRequest
from app.repositories.discovery import DiscoveryRepository
from app.services.cache import SQLiteCache
from app.services.discovery import DiscoveryService
from tests.test_discovery_presets import _preset_payload, _result, _seed_run


@pytest.fixture
def discovery_state(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config_settings._SHELL_ENV_VALUES", {})
    cache = SQLiteCache(settings=Settings(cache_path=tmp_path / "discovery.sqlite3", scheduler_enabled=False, llm_enabled=False))
    run_id = _seed_run(cache.path, rule_version="discovery-v1", rows=[
        _result("600001.SH", rank=1, market="SH", score=92, quality=90),
        _result("600002.SH", rank=2, market="SH", score=91, quality=90),
    ])
    app = FastAPI()
    app.include_router(discovery.router)
    app.include_router(watchlist.router)
    app.dependency_overrides[get_domain_services] = lambda: cache.domain_services
    app.dependency_overrides[get_datahub] = lambda: SimpleNamespace(cache=cache)
    with TestClient(app) as client:
        response = client.post("/api/discovery/presets", json=_preset_payload().model_dump(mode="json"))
        assert response.status_code == 201
        preset = response.json()
        payload = {"run_id": run_id, "expected_preset_revision": preset["revision"], "symbols": ["600001.SH"]}
        yield client, cache, preset, payload


@pytest.mark.parametrize("status", ["to_research", "watching", "excluded", "holding_research"])
def test_exact_http_enqueue_retry_does_not_repeat_user_state_effects(discovery_state, status):
    client, cache, preset, payload = discovery_state
    endpoint = f"/api/discovery/presets/{preset['id']}/research-queue"
    first = client.post(endpoint, json=payload)
    assert first.status_code == 200 and first.json()["added_count"] == 1
    edited = client.patch("/api/watchlist/600001.SH", json={
        "research_status": status, "note": "用户完成复核", "priority": "high", "pinned": True,
        "next_review_date": "2026-09-10", "group_name": "复核组",
    })
    assert edited.status_code == 200
    before = cache.watchlist_item("600001.SH")
    repeated = client.post(endpoint, json=payload)
    assert repeated.status_code == 200
    assert repeated.json()["added_count"] == 0 and repeated.json()["existing_count"] == 1
    assert repeated.json()["items"][0]["enqueued_at"] == first.json()["items"][0]["enqueued_at"]
    assert cache.watchlist_item("600001.SH") == before


@pytest.mark.parametrize("new_source", ["run", "preset", "revision"])
def test_genuinely_new_source_keeps_original_promotion_behavior(discovery_state, new_source):
    client, cache, preset, payload = discovery_state
    endpoint = f"/api/discovery/presets/{preset['id']}/research-queue"
    assert client.post(endpoint, json=payload).json()["added_count"] == 1
    assert client.patch("/api/watchlist/600001.SH", json={"research_status": "excluded", "note": "用户笔记"}).status_code == 200
    if new_source == "run":
        run_id = _seed_run(cache.path, rule_version="discovery-v1", rows=[_result("600001.SH", rank=1, market="SH", score=92, quality=90)])
        payload = dict(payload, run_id=run_id)
    elif new_source == "preset":
        created = client.post("/api/discovery/presets", json=_preset_payload("另一方案").model_dump(mode="json"))
        assert created.status_code == 201
        endpoint = f"/api/discovery/presets/{created.json()['id']}/research-queue"
    else:
        renamed = client.patch(f"/api/discovery/presets/{preset['id']}", json={"name": "新版方案", "expected_revision": 1})
        assert renamed.status_code == 200
        payload = dict(payload, expected_preset_revision=2)
    result = client.post(endpoint, json=payload)
    assert result.status_code == 200 and result.json()["added_count"] == 1
    stored = cache.watchlist_item("600001.SH")
    assert stored.research_status == "to_research" and stored.note == "用户笔记"
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM discovery_research_queue_source").fetchone()[0] == 2


def test_mixed_duplicate_and_new_batch_preserves_duplicate_state(discovery_state):
    client, cache, preset, payload = discovery_state
    endpoint = f"/api/discovery/presets/{preset['id']}/research-queue"
    assert client.post(endpoint, json=payload).status_code == 200
    assert client.patch("/api/watchlist/600001.SH", json={"research_status": "excluded"}).status_code == 200
    before = cache.watchlist_item("600001.SH")
    response = client.post(endpoint, json=dict(payload, symbols=["600001.SH", "600002.SH"]))
    assert response.status_code == 200
    assert response.json()["added_count"] == response.json()["existing_count"] == 1
    assert cache.watchlist_item("600001.SH") == before
    assert cache.watchlist_item("600002.SH").research_status == "to_research"


def test_duplicate_provenance_does_not_bypass_revision_or_membership_admission(discovery_state):
    client, cache, preset, payload = discovery_state
    endpoint = f"/api/discovery/presets/{preset['id']}/research-queue"
    assert client.post(endpoint, json=payload).status_code == 200
    before = cache.watchlist_item("600001.SH")
    rejected = client.post(endpoint, json=dict(payload, symbols=["600001.SH", "600999.SH"]))
    assert rejected.status_code == 400
    assert client.patch(f"/api/discovery/presets/{preset['id']}", json={"name": "新版方案", "expected_revision": 1}).status_code == 200
    stale = client.post(endpoint, json=payload)
    assert stale.status_code == 400 and "修订" in stale.json()["detail"]
    assert cache.watchlist_item("600001.SH") == before
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM discovery_research_queue_source").fetchone()[0] == 1


def test_failure_in_new_source_rolls_back_entire_mixed_batch(discovery_state):
    client, cache, preset, payload = discovery_state
    endpoint = f"/api/discovery/presets/{preset['id']}/research-queue"
    assert client.post(endpoint, json=payload).status_code == 200
    assert client.patch("/api/watchlist/600001.SH", json={"research_status": "watching"}).status_code == 200
    before = cache.watchlist_item("600001.SH")
    with sqlite3.connect(cache.path) as conn:
        conn.execute("""CREATE TRIGGER reject_second_source AFTER INSERT ON discovery_research_queue_source
            WHEN NEW.symbol = '600002.SH' BEGIN SELECT RAISE(ABORT, 'synthetic source failure'); END""")
    failed = client.post(endpoint, json=dict(payload, symbols=["600001.SH", "600002.SH"]))
    assert failed.status_code == 503
    assert cache.watchlist_item("600001.SH") == before
    assert cache.watchlist_item("600002.SH") is None
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM discovery_research_queue_source").fetchone()[0] == 1


def test_two_connections_enqueue_identical_source_once(discovery_state):
    from app.repositories.discovery import _upsert_research_watchlist

    _client, cache, preset, payload = discovery_state
    first_service = cache.domain_services.discovery
    second_service = DiscoveryService(DiscoveryRepository(cache.path))
    request = DiscoveryResearchQueueRequest.model_validate(payload)
    entered, release, second_started = Event(), Event(), Event()

    def pause_first(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return _upsert_research_watchlist(*args, **kwargs)

    def enqueue_second():
        second_started.set()
        return second_service.enqueue_research(preset["id"], request)

    with patch("app.repositories.discovery._upsert_research_watchlist", side_effect=pause_first):
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(first_service.enqueue_research, preset["id"], request)
            try:
                assert entered.wait(2)
                second = workers.submit(enqueue_second)
                assert second_started.wait(2)
                with pytest.raises(FutureTimeoutError):
                    second.result(timeout=0.1)
            finally:
                release.set()
            initial, repeated = first.result(timeout=5), second.result(timeout=5)
    assert initial.added_count == 1 and repeated.existing_count == 1
    assert initial.items[0].enqueued_at == repeated.items[0].enqueued_at
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM discovery_research_queue_source").fetchone()[0] == 1
