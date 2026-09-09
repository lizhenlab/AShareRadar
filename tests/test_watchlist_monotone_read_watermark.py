"""A delayed older page acknowledgment must not resurrect read changes."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import Event
from unittest.mock import patch

import pytest

from app.config import Settings
from app.services.cache import SQLiteCache
from tests.test_alert_rule_mutation_transactions import _client_cache
from tests.test_local_lifecycle import _analysis_for_advice


def _history(cache):
    analysis = _analysis_for_advice()
    cache.save_watchlist_item(analysis.quote)
    first = cache.save_advice_snapshot(analysis)
    step = -1 if analysis.action_advice.confidence >= 98 else 1
    def append_change(number, destination=None):
        return (destination or cache).save_advice_snapshot(analysis.model_copy(update={
            "action_advice": analysis.action_advice.model_copy(update={"confidence": analysis.action_advice.confidence + step * number}),
        }))
    second, third = append_change(1), append_change(2)
    return analysis.quote.code, (first, second, third), append_change


def test_older_http_read_ack_cannot_restore_cleared_unread_changes(tmp_path):
    client, cache = _client_cache(tmp_path)
    symbol, rows, append_change = _history(cache)
    endpoint = f"/api/watchlist/{symbol}/mark-viewed"
    assert cache.watchlist_item(symbol).unread_change_count == 2
    with client:
        latest = client.post(endpoint, json={"viewed_through_advice_id": rows[-1].id})
        stale = client.post(endpoint, json={"viewed_through_advice_id": rows[1].id})
        assert latest.status_code == stale.status_code == 200
        assert latest.json()["unread_change_count"] == stale.json()["unread_change_count"] == 0
        append_change(3)
        repeated = client.post(endpoint, json={"viewed_through_advice_id": rows[1].id})
        assert repeated.json()["unread_change_count"] == 1
        assert cache.watchlist_item(symbol).unread_change_count == 1


@pytest.mark.parametrize("order,expected", [
    ((1, 2, 0, 1), (1, 0, 0, 0)),
    ((2, 0, 1, 2), (0, 0, 0, 0)),
    ((0, 1, 0, 2), (2, 1, 1, 0)),
])
def test_http_read_ack_permutations_only_reduce_existing_unread_count(tmp_path, order, expected):
    client, cache = _client_cache(tmp_path)
    symbol, rows, _ = _history(cache)
    with client:
        for index, remaining in zip(order, expected, strict=True):
            response = client.post(f"/api/watchlist/{symbol}/mark-viewed", json={"viewed_through_advice_id": rows[index].id})
            assert response.status_code == 200
            assert response.json()["unread_change_count"] == remaining
    assert cache.watchlist_item(symbol).unread_change_count == expected[-1]


def test_invalid_and_foreign_http_watermarks_leave_entire_row_unchanged(tmp_path):
    client, cache = _client_cache(tmp_path)
    symbol, _, _ = _history(cache)
    analysis = _analysis_for_advice()
    foreign = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"code": "000001", "market": "SZ"})})
    foreign_row = cache.save_advice_snapshot(foreign)
    current = cache.watchlist_item(symbol)
    with client:
        for watermark, expected_status in [(foreign_row.id, 400), (foreign_row.id + 10_000, 400), (0, 422), (-1, 422)]:
            response = client.post(f"/api/watchlist/{symbol}/mark-viewed", json={"viewed_through_advice_id": watermark})
            assert response.status_code == expected_status
            assert cache.watchlist_item(symbol) == current


@pytest.mark.parametrize("payload", [{}, {"clear_unread": False, "viewed_through_advice_id": 3}])
def test_view_without_ack_preserves_unread_count(tmp_path, payload):
    client, cache = _client_cache(tmp_path)
    symbol, rows, _ = _history(cache)
    if "viewed_through_advice_id" in payload:
        payload = dict(payload, viewed_through_advice_id=rows[-1].id)
    with client:
        response = client.post(f"/api/watchlist/{symbol}/mark-viewed", json=payload)
    assert response.status_code == 200
    assert response.json()["unread_change_count"] == 2
    assert response.json()["last_viewed_at"] is not None


def test_stale_ack_and_concurrent_new_advice_keep_only_real_unread_changes(tmp_path):
    from app.repositories.watchlist import _unread_change_count_after_watermark

    client, cache = _client_cache(tmp_path)
    writer = SQLiteCache(settings=Settings(
        cache_path=cache.path, scheduler_enabled=False, llm_enabled=False, advice_history_dedupe_seconds=0,
    ))
    symbol, rows, append_change = _history(cache)
    cache.mark_watchlist_viewed(symbol, viewed_through_advice_id=rows[-1].id)
    append_change(3)
    entered, release, writer_started = Event(), Event(), Event()

    def paused_count(*args, **kwargs):
        remaining = _unread_change_count_after_watermark(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return remaining

    def append_during_ack():
        writer_started.set()
        return append_change(4, writer)

    with client, patch("app.repositories.watchlist._unread_change_count_after_watermark", side_effect=paused_count):
        with ThreadPoolExecutor(max_workers=2) as workers:
            acknowledge = workers.submit(client.post, f"/api/watchlist/{symbol}/mark-viewed", json={"viewed_through_advice_id": rows[1].id})
            try:
                assert entered.wait(2)
                save = workers.submit(append_during_ack)
                assert writer_started.wait(2)
                with pytest.raises(FutureTimeoutError):
                    save.result(timeout=0.1)
            finally:
                release.set()
            assert acknowledge.result(timeout=5).status_code == 200
            assert save.result(timeout=5).id > rows[-1].id
    assert cache.watchlist_item(symbol).unread_change_count == 2
    current = cache.mark_watchlist_viewed(symbol, viewed_through_advice_id=rows[1].id)
    assert current.unread_change_count == 2
