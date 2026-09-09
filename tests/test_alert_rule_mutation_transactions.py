"""Concurrent valid alert edits cannot persist an invalid merged condition."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import sqlite3
from threading import Event
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_datahub
from app.api.routes import alerts, watchlist
from app.config import Settings
from app.models.user_data import AlertRuleInput, AlertRuleUpdate
from app.services.alerts import validate_alert_condition
from app.services.cache import SQLiteCache
from tests.factories import make_quote


def _client_cache(tmp_path):
    path = tmp_path / "persistent-user-state.sqlite3"
    cache = SQLiteCache(settings=Settings(cache_path=path, scheduler_enabled=False, llm_enabled=False, advice_history_dedupe_seconds=0))
    class Hub:
        async def quote(self, _symbol):
            return make_quote()
    hub = Hub()
    hub.cache = cache
    app = FastAPI()
    app.include_router(alerts.router)
    app.include_router(watchlist.router)
    app.dependency_overrides[get_datahub] = lambda: hub
    return TestClient(app), cache


@pytest.mark.parametrize("delayed_payload,first_payload,expected_type,expected_threshold", [
    ({"threshold": 500}, {"condition_type": "trend_score_above"}, "trend_score_above", 10),
    ({"condition_type": "trend_score_above"}, {"threshold": 500}, "price_above", 500),
])
def test_http_patch_revalidates_condition_after_a_concurrent_valid_edit(
    tmp_path, delayed_payload, first_payload, expected_type, expected_threshold,
):
    client, cache = _client_cache(tmp_path)
    with client:
        created = client.post("/api/alerts", json={"symbol": "600519", "condition_type": "price_above", "threshold": 10}).json()
        endpoint = f"/api/alerts/{created['id']}"
        entered, release = Event(), Event()
        original = cache.update_alert_rule
        def delay_threshold(row_id, payload):
            if payload.model_dump(exclude_unset=True) == delayed_payload:
                entered.set()
                assert release.wait(5)
            return original(row_id, payload)
        with patch.object(cache, "update_alert_rule", side_effect=delay_threshold), ThreadPoolExecutor(max_workers=1) as workers:
            delayed = workers.submit(client.patch, endpoint, json=delayed_payload)
            try:
                assert entered.wait(2)
                newer = client.patch(endpoint, json=first_payload)
            finally:
                release.set()
            stale = delayed.result(timeout=5)
        assert newer.status_code == 200
        assert stale.status_code == 400
        assert "0到100" in stale.json()["detail"]
        stored = cache.alert_rule(created["id"])
        assert stored.condition_type == expected_type and stored.threshold == expected_threshold
        validate_alert_condition(stored.condition_type, stored.threshold)
        assert cache.alert_events() == []


@pytest.mark.parametrize("condition_type,threshold", [
    ("price_above", 0.01), ("price_below", 0.01),
    ("trend_score_above", 0), ("trend_score_below", 100),
    ("change_pct_above", -100), ("change_pct_below", 100),
    ("break_support", 0), ("break_resistance", 0),
])
def test_public_and_repository_admission_keep_existing_valid_boundaries(tmp_path, condition_type, threshold):
    client, cache = _client_cache(tmp_path)
    validate_alert_condition(condition_type, threshold)
    created = cache.create_alert_rule(make_quote(), AlertRuleInput(
        symbol="600519", condition_type=condition_type, threshold=threshold,
    ))
    with client:
        response = client.patch(f"/api/alerts/{created.id}", json={
            "condition_type": condition_type, "threshold": threshold, "name": " ",
        })
    assert response.status_code == 200
    assert response.json()["threshold"] == threshold
    assert response.json()["name"] == created.name


@pytest.mark.parametrize("condition_type,threshold", [
    ("price_above", 0), ("price_below", -1),
    ("trend_score_above", -0.01), ("trend_score_below", 100.01),
    ("change_pct_above", -100.01), ("change_pct_below", 100.01),
    ("break_support", -0.01), ("break_resistance", -0.01),
    ("", 10), ("unknown", 10),
])
def test_invalid_condition_cannot_enter_through_http_or_cache(tmp_path, condition_type, threshold):
    client, cache = _client_cache(tmp_path)
    with pytest.raises(ValueError):
        cache.create_alert_rule(make_quote(), AlertRuleInput(
            symbol="600519", condition_type=condition_type, threshold=threshold,
        ))
    assert cache.alert_rules() == []
    current = cache.create_alert_rule(make_quote(), AlertRuleInput(
        symbol="600519", condition_type="price_above", threshold=10,
    ))
    payload = {"condition_type": condition_type, "threshold": threshold, "name": "invalid mutation"}
    with pytest.raises(ValueError):
        cache.update_alert_rule(current.id, AlertRuleUpdate(**payload))
    with client:
        response = client.patch(f"/api/alerts/{current.id}", json=payload)
    assert response.status_code == 400
    assert cache.alert_rule(current.id) == current


def test_database_failure_rolls_back_rule_and_observation_state(tmp_path):
    client, cache = _client_cache(tmp_path)
    created = cache.create_alert_rule(make_quote(), AlertRuleInput(
        symbol="600519", condition_type="price_above", threshold=10,
    ))
    cache.update_alert_rule_state(
        created, checked_at="2026-09-08 10:00:00", state="触发", triggered=True,
        message="synthetic trigger", quote=make_quote(),
    )
    current, events = cache.alert_rule(created.id), cache.alert_events()
    with sqlite3.connect(cache.path) as conn:
        conn.execute("""CREATE TRIGGER reject_rule_update AFTER UPDATE ON alert_rule
            BEGIN SELECT RAISE(ABORT, 'synthetic update failure'); END""")
    with client:
        response = client.patch(f"/api/alerts/{created.id}", json={"threshold": 20, "name": " "})
    assert response.status_code == 503
    assert cache.alert_rule(created.id) == current
    assert cache.alert_events() == events
    with sqlite3.connect(cache.path) as conn:
        conn.execute("DROP TRIGGER reject_rule_update")
    updated = cache.update_alert_rule(created.id, AlertRuleUpdate(threshold=20, name=" "))
    assert updated.name == "价格上穿 20"
    assert updated.last_checked_at is updated.last_triggered_at is None
    assert updated.last_state == "等待" and updated.trigger_count == current.trigger_count


@pytest.mark.parametrize("bad_threshold", ["invalid", float("inf")])
def test_semantic_edit_cannot_validate_a_legacy_display_fallback(tmp_path, bad_threshold):
    client, cache = _client_cache(tmp_path)
    created = cache.create_alert_rule(make_quote(), AlertRuleInput(
        symbol="600519", condition_type="price_above", threshold=10,
    ))
    with sqlite3.connect(cache.path) as conn:
        conn.execute("UPDATE alert_rule SET threshold = ? WHERE id = ?", (bad_threshold, created.id))
    with client:
        metadata = client.patch(f"/api/alerts/{created.id}", json={"note": "needs repair", "enabled": False})
        before = cache.alert_rule(created.id)
        invalid = client.patch(f"/api/alerts/{created.id}", json={"condition_type": "break_support"})
        assert cache.alert_rule(created.id) == before
        repaired = client.patch(f"/api/alerts/{created.id}", json={"condition_type": "break_support", "threshold": 0})
    assert metadata.status_code == 200
    assert invalid.status_code == 400
    assert before.note == "needs repair" and not before.enabled
    assert repaired.status_code == 200 and repaired.json()["threshold"] == 0


def test_merged_validation_holds_database_write_lock_across_cache_instances(tmp_path):
    from app.repositories.alerts import _validate_merged_rule_condition

    _, first_cache = _client_cache(tmp_path)
    second_cache = SQLiteCache(settings=Settings(
        cache_path=first_cache.path, scheduler_enabled=False, llm_enabled=False,
    ))
    created = first_cache.create_alert_rule(make_quote(), AlertRuleInput(
        symbol="600519", condition_type="price_above", threshold=10,
    ))
    entered, release, second_started = Event(), Event(), Event()

    def paused_validation(updates, row):
        if any(item.column == "condition_type" for item in updates):
            entered.set()
            assert release.wait(5)
        return _validate_merged_rule_condition(updates, row)

    def update_threshold():
        second_started.set()
        return second_cache.update_alert_rule(created.id, AlertRuleUpdate(threshold=500))

    with patch("app.repositories.alerts._validate_merged_rule_condition", side_effect=paused_validation):
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(first_cache.update_alert_rule, created.id, AlertRuleUpdate(condition_type="trend_score_above"))
            try:
                assert entered.wait(2)
                second = workers.submit(update_threshold)
                assert second_started.wait(2)
                with pytest.raises(FutureTimeoutError):
                    second.result(timeout=0.1)
            finally:
                release.set()
            assert first.result(timeout=5).condition_type == "trend_score_above"
            with pytest.raises(ValueError, match="0到100"):
                second.result(timeout=5)
    stored = second_cache.alert_rule(created.id)
    assert stored.condition_type == "trend_score_above" and stored.threshold == 10
