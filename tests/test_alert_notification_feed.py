from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Event

import pytest

from app.config import Settings
from app.db.alert_stream import read_alert_stream_state, rotate_alert_stream_state
from app.repositories import alerts as alert_repository
from app.services.cache import SQLiteCache
from tests.factories import make_quote
from tests.test_api_alert_routes import _client, _DataHubStub, _create_alert_events


URL = "/api/alerts/notification-events"


def _cache(tmp_path):
    path = tmp_path / "isolated.sqlite3"
    return SQLiteCache(path, settings=Settings(cache_path=path, scheduler_enabled=False, llm_enabled=False))


def _append(cache, rule_id, message="new live event"):
    rule = cache.alert_rule(rule_id)
    assert rule is not None
    event = cache.update_alert_rule_state(
        rule, checked_at="2026-09-09 10:00:00", state="触发", triggered=True,
        message=message, quote=make_quote(), force_event=True,
    )
    assert event is not None
    return event


def test_bootstrap_skips_history_then_pages_new_events_and_preserves_normal_restart(tmp_path):
    cache = _cache(tmp_path)
    events = _create_alert_events(cache)
    with _client(_DataHubStub(cache=cache)) as client:
        baseline = client.get(URL)
        assert baseline.status_code == 200
        assert baseline.headers["cache-control"] == "no-store"
        page = baseline.json()
        assert page == dict(stream_id=page["stream_id"], baseline_id=0, cursor_id=4, reset=True, has_more=False, events=[])
        expected = [_append(cache, events[0].rule_id).id for _ in range(5)]
        first = client.get(URL, params=dict(stream_id=page["stream_id"], after_id=4, limit=3)).json()
        second = client.get(URL, params=dict(stream_id=page["stream_id"], after_id=first["cursor_id"], limit=3)).json()
        empty = client.get(URL, params=dict(stream_id=page["stream_id"], after_id=second["cursor_id"])).json()
    assert [item["id"] for item in first["events"] + second["events"]] == expected
    assert first["has_more"] is True and second["has_more"] is False
    assert not first["reset"] and first["baseline_id"] == 0
    assert empty["events"] == [] and empty["cursor_id"] == expected[-1] and not empty["has_more"]
    reopened = _cache(tmp_path)
    with _client(_DataHubStub(cache=reopened)) as client:
        assert client.get(URL).json()["stream_id"] == page["stream_id"]


@pytest.mark.parametrize("old_cursor", [2, 4, 100])
def test_new_stream_ignores_old_history_id_and_delivers_post_cutover_events(tmp_path, old_cursor):
    cache = _cache(tmp_path)
    events = _create_alert_events(cache)
    with sqlite3.connect(cache.path) as conn:
        previous = read_alert_stream_state(conn)
        conn.execute("BEGIN IMMEDIATE")
        current = rotate_alert_stream_state(conn)
    new = _append(cache, events[0].rule_id)
    with _client(_DataHubStub(cache=cache)) as client:
        response = client.get(URL, params=dict(stream_id=previous.stream_id, after_id=old_cursor))
    assert response.status_code == 200
    page = response.json()
    assert page["stream_id"] == current.stream_id != previous.stream_id
    assert page["baseline_id"] == 4 and page["cursor_id"] == new.id
    assert page["reset"] and [event["id"] for event in page["events"]] == [new.id]


def test_empty_retained_event_table_keeps_sequence_and_does_not_reset_stream(tmp_path):
    cache = _cache(tmp_path)
    events = _create_alert_events(cache)
    with _client(_DataHubStub(cache=cache)) as client:
        before = client.get(URL).json()
        with sqlite3.connect(cache.path) as conn:
            conn.execute("DELETE FROM alert_event")
        empty = client.get(URL, params=dict(stream_id=before["stream_id"], after_id=4)).json()
        bootstrap = client.get(URL).json()
        new = _append(cache, events[0].rule_id)
        page = client.get(URL, params=dict(stream_id=before["stream_id"], after_id=4)).json()
    assert empty["stream_id"] == bootstrap["stream_id"] == before["stream_id"]
    assert empty["cursor_id"] == bootstrap["cursor_id"] == 4
    assert not empty["reset"] and empty["events"] == []
    assert new.id == 5 and [row["id"] for row in page["events"]] == [5]


@pytest.mark.parametrize("params", [
    {"after_id": 0}, {"stream_id": "a" * 32},
    {"stream_id": "bad", "after_id": 0}, {"stream_id": "A" * 32, "after_id": 0},
    {"stream_id": "a" * 32, "after_id": -1}, {"stream_id": "a" * 32, "after_id": 9_007_199_254_740_992},
    {"limit": 0}, {"limit": 501},
])
def test_notification_feed_rejects_invalid_cursor_parameters(tmp_path, params):
    with _client(_DataHubStub(cache=_cache(tmp_path))) as client:
        assert client.get(URL, params=params).status_code == 422


def test_same_stream_invalid_range_is_rejected_without_rebinding(tmp_path):
    cache = _cache(tmp_path)
    _create_alert_events(cache)
    with sqlite3.connect(cache.path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = rotate_alert_stream_state(conn)
    with _client(_DataHubStub(cache=cache)) as client:
        for cursor in (0, 5):
            response = client.get(URL, params=dict(stream_id=current.stream_id, after_id=cursor))
            assert response.status_code == 400
        assert client.get(URL).json()["stream_id"] == current.stream_id


@pytest.mark.parametrize("damage", ["missing-row", "future-baseline", "extra-trigger"])
def test_malformed_stream_metadata_fails_closed_without_creating_a_new_stream(tmp_path, damage):
    cache = _cache(tmp_path)
    with sqlite3.connect(cache.path) as conn:
        old = read_alert_stream_state(conn)
        if damage == "missing-row":
            conn.execute("DELETE FROM alert_stream_state")
        elif damage == "future-baseline":
            conn.execute("UPDATE alert_stream_state SET baseline_event_id = 123")
        else:
            conn.execute("CREATE TRIGGER extra_stream_trigger AFTER UPDATE ON alert_stream_state BEGIN SELECT 1; END")
    with _client(_DataHubStub(cache=cache)) as client:
        assert client.get(URL).status_code == 503
    with sqlite3.connect(cache.path) as conn:
        row = conn.execute("SELECT stream_id FROM alert_stream_state").fetchone()
        assert row is None if damage == "missing-row" else row[0] == old.stream_id


def test_page_identity_and_events_are_read_from_one_snapshot_during_cutover(tmp_path, monkeypatch):
    cache = _cache(tmp_path)
    _create_alert_events(cache)
    with sqlite3.connect(cache.path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        old = read_alert_stream_state(conn)
    entered, release = Event(), Event()
    read_state = alert_repository.read_alert_stream_state

    def pause_after_metadata(conn):
        state = read_state(conn)
        entered.set()
        assert release.wait(timeout=5)
        return state

    monkeypatch.setattr(alert_repository, "read_alert_stream_state", pause_after_metadata)
    with _client(_DataHubStub(cache=cache)) as client, ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(client.get, URL, params=dict(stream_id=old.stream_id, after_id=0))
        try:
            assert entered.wait(timeout=5)
            with sqlite3.connect(cache.path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE alert_event SET message='replacement history'")
                current = rotate_alert_stream_state(conn)
        finally:
            release.set()
        response = pending.result(timeout=5)
    assert response.status_code == 200
    page = response.json()
    assert page["stream_id"] == old.stream_id != current.stream_id and not page["reset"]
    assert [row["message"] for row in page["events"]] == [f"测试触发 {i}" for i in range(1, 5)]
    with sqlite3.connect(cache.path) as conn:
        assert read_alert_stream_state(conn).stream_id == current.stream_id


def test_old_event_listing_remains_available_with_its_existing_contract(tmp_path):
    cache = _cache(tmp_path)
    _create_alert_events(cache)
    with _client(_DataHubStub(cache=cache)) as client:
        recent = client.get("/api/alerts/events").json()
        incremental = client.get("/api/alerts/events", params={"after_id": 2}).json()
    assert [row["id"] for row in recent] == [4, 3, 2, 1]
    assert [row["id"] for row in incremental] == [3, 4]
