"""Due queue pages describe one verified, filtered SQLite read snapshot."""

from datetime import datetime
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_datahub
from app.api.routes.reviews import router
from app.models.reviews import AdviceReviewPlanUpdate
from app.repositories import advice_reviews
from app.services import advice_review, trading_calendar
from app.services.cache import SQLiteCache
from app.services.research_replay import evaluate_advice_forward_window
from app.utils.exchange_calendar_contract import bundled_exchange_sessions
from tests.test_advice_reviews import _insert_advice, _plan_input, make_kline


AS_OF = "2026-07-17T16:00:00"


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", tmp_path / "absent-calendar.json")
    monkeypatch.setattr(advice_review, "market_now_naive", lambda: datetime(2026, 7, 21, 16))
    cache = SQLiteCache(tmp_path / "queue.sqlite3")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_datahub] = lambda: SimpleNamespace(cache=cache)
    with TestClient(app) as client:
        yield cache, client


def _create(cache: SQLiteCache, *, snapshot="2026-07-01 15:00:00", horizon=3):
    advice_id = _insert_advice(Path(cache.path), market_time=snapshot)
    return cache.create_advice_review_plan(_plan_input(advice_id).model_copy(update={"horizon_days": horizon}))


@pytest.mark.parametrize("count", [0, 1, 200, 201, 501])
def test_due_pages_reach_every_eligible_plan_and_report_complete_counts(queue, count):
    cache, client = queue
    plans = [_create(cache) for _ in range(count)]
    response = client.get("/api/reviews/due", params={"as_of": AS_OF, "page_size": 200})
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict), "bounded bare list cannot describe the complete due queue"
    assert body["total"] == count
    assert body["page_count"] == (count + 199) // 200
    assert body["as_of"] == "2026-07-17 15:15:00"
    assert body["page"] == 1 and body["page_size"] == 200
    ids = [item["plan"]["id"] for item in body["items"]]
    for page in range(2, body["page_count"] + 1):
        response = client.get("/api/reviews/due", params={
            "as_of": body["as_of"], "snapshot_token": body["snapshot_token"], "page": page, "page_size": 200,
        })
        assert response.status_code == 200
        continuation = response.json()
        assert continuation["snapshot_token"] == body["snapshot_token"]
        assert continuation["total"] == count and continuation["page"] == page
        ids.extend(item["plan"]["id"] for item in continuation["items"])
    assert ids == [plan.id for plan in plans]
    assert response.headers["cache-control"] == "no-store"


def test_due_filters_apply_before_count_and_pagination(queue):
    cache, client = queue
    _create(cache, snapshot="2026-06-01 15:00:00", horizon=1)
    _create(cache, horizon=2)
    selected = _create(cache, horizon=3)
    response = client.get("/api/reviews/due", params={
        "as_of": AS_OF, "page_size": 1, "symbol": "  519.sh  ", "from_date": "2026-07-01", "horizon_days": 3,
    })
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict), "filtered due pages need a complete response contract"
    assert body["total"] == 1 and body["page_count"] == 1
    assert [item["plan"]["id"] for item in body["items"]] == [selected.id]


def _first(client, **params):
    response = client.get("/api/reviews/due", params={"as_of": AS_OF, "page_size": 1, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _next(client, first, **params):
    return client.get("/api/reviews/due", params={
        "as_of": first["as_of"], "snapshot_token": first["snapshot_token"],
        "page": 2, "page_size": first["page_size"], **params,
    })


def _evaluation(plan, *, complete=False):
    rows = [make_kline(date=session.isoformat(), close=100, high=101, low=99)
            for session in sorted(bundled_exchange_sessions())
            if "2026-05-08" <= session.isoformat() <= "2026-07-06"] if complete else []
    return evaluate_advice_forward_window(
        plan, rows, as_of=datetime(2026, 7, 6 if complete else 17, 15, 15),
        evaluated_at="2026-07-17T08:01:00.000000Z",
    )


@pytest.mark.parametrize("change", ["create", "archive", "revise", "evaluation"])
def test_continuation_rejects_a_changed_queue_instead_of_shifting_pages(queue, change):
    cache, client = queue
    plan = _create(cache)
    _create(cache)
    first = _first(client)
    if change == "create":
        _create(cache)
    elif change == "archive":
        cache.delete_advice_review_plan(plan.id, expected_revision=plan.revision)
    elif change == "revise":
        cache.update_advice_review_plan(plan.id, AdviceReviewPlanUpdate(expected_revision=1, hypothesis="新的假设"))
    else:
        saved = cache.save_advice_review_evaluation(_evaluation(plan))
        assert saved.status == "insufficient"
    response = _next(client, first)
    assert response.status_code == 409
    assert "队列已变化" in response.json()["detail"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("params", [
    {"page_size": 2}, {"symbol": "SH"}, {"from_date": "2000-01-01"},
    {"horizon_days": 3}, {"as_of": "2026-07-20 15:15:00"},
])
def test_token_binds_filters_page_size_and_mature_as_of_even_if_rows_still_match(queue, params):
    cache, client = queue
    _create(cache)
    _create(cache)
    response = _next(client, _first(client), **params)
    assert response.status_code == 409


def test_canonical_filters_and_equivalent_timezones_share_the_same_identity(queue):
    cache, client = queue
    _create(cache)
    _create(cache)
    first = _first(client, symbol=" 519.sh ")
    same = _first(client, symbol="519.SH", as_of="2026-07-17T08:00:00Z")
    assert first["snapshot_token"] == same["snapshot_token"]
    response = _next(client, first, symbol="519.SH", as_of="2026-07-17T07:15:00Z")
    assert response.status_code == 200
    assert response.json()["as_of"] == first["as_of"]


@pytest.mark.parametrize("params,status", [
    ({"page": 0}, 422), ({"page_size": 201}, 422), ({"page_size": 0}, 422),
    ({"snapshot_token": "bad"}, 422), ({"horizon_days": 61}, 422), ({"from_date": "bad"}, 422),
    ({"page": 2}, 400), ({"page": 2, "as_of": AS_OF}, 400),
    ({"page": 2, "snapshot_token": "0" * 64}, 400),
    ({"snapshot_token": "0" * 64, "as_of": AS_OF}, 400),
    ({"as_of": "2026-07-22T16:00:00"}, 400),
])
def test_invalid_page_requests_are_rejected_without_writes(queue, params, status):
    cache, client = queue
    _create(cache)
    with sqlite3.connect(cache.path) as conn:
        before = list(conn.iterdump())
    response = client.get("/api/reviews/due", params=params)
    assert response.status_code == status
    with sqlite3.connect(cache.path) as conn:
        assert list(conn.iterdump()) == before


def test_valid_identity_with_out_of_range_page_is_not_an_empty_success(queue):
    cache, client = queue
    _create(cache)
    response = _next(client, _first(client))
    assert response.status_code == 400
    assert "页码超出范围" in response.json()["detail"]


@pytest.mark.parametrize("requested,expected", [
    ("2026-07-17T15:14:59", "2026-07-16 15:15:00"),
    ("2026-07-17T15:15:00", "2026-07-17 15:15:00"),
    ("2026-07-18T16:00:00", "2026-07-17 15:15:00"),
    ("2026-07-19T08:00:00Z", "2026-07-17 15:15:00"),
])
def test_queue_reports_the_actual_completed_session_cutoff(queue, requested, expected):
    cache, client = queue
    plan = _create(cache, snapshot="2026-07-16 15:15:00", horizon=1)
    body = _first(client, as_of=requested)
    assert body["as_of"] == expected
    assert [item["plan"]["id"] for item in body["items"]] == ([] if expected.startswith("2026-07-16") else [plan.id])


def test_current_evaluated_and_archived_plans_are_excluded_but_insufficient_and_revised_remain(queue):
    cache, client = queue
    evaluated, insufficient, archived = [_create(cache) for _ in range(3)]
    assert cache.save_advice_review_evaluation(_evaluation(evaluated, complete=True)).status == "evaluated"
    assert cache.save_advice_review_evaluation(_evaluation(insufficient)).status == "insufficient"
    cache.delete_advice_review_plan(archived.id, expected_revision=1)
    assert [item["plan"]["id"] for item in _first(client)["items"]] == [insufficient.id]
    cache.update_advice_review_plan(evaluated.id, AdviceReviewPlanUpdate(expected_revision=1, hypothesis="新修订重新观察"))
    body = _first(client, page_size=200)
    assert [item["plan"]["id"] for item in body["items"]] == [evaluated.id, insufficient.id]
    assert body["items"][0]["latest_evaluation"] is None


def test_queue_order_uses_due_date_then_id_and_ignores_unmatured_plans(queue):
    cache, client = queue
    later = _create(cache, horizon=5)
    earlier = _create(cache, horizon=1)
    same_day = _create(cache, horizon=1)
    _create(cache, snapshot="2026-07-16 15:15:00", horizon=60)
    body = _first(client, page_size=200)
    assert [item["plan"]["id"] for item in body["items"]] == [earlier.id, same_day.id, later.id]
    assert body["total"] == 3


def test_plan_and_evaluation_reads_share_one_query_only_transaction(queue, monkeypatch):
    cache, client = queue
    plan = _create(cache)
    _create(cache)
    other = SQLiteCache(cache.path)
    original = advice_reviews._latest_result_rows
    transactions = []

    def interleave(conn, plans):
        transactions.append((conn.in_transaction, conn.execute("PRAGMA query_only").fetchone()[0]))
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE advice_review_plan SET hypothesis=hypothesis")
        other.save_advice_review_evaluation(_evaluation(plan))
        return original(conn, plans)

    monkeypatch.setattr(advice_reviews, "_latest_result_rows", interleave)
    first = _first(client)
    assert transactions == [(True, 1)]
    assert first["items"][0]["latest_evaluation"] is None
    monkeypatch.setattr(advice_reviews, "_latest_result_rows", original)
    assert _next(client, first).status_code == 409


def test_corrupt_plan_ledger_fails_instead_of_returning_a_partial_complete_queue(queue):
    cache, client = queue
    plan = _create(cache)
    _create(cache)
    with sqlite3.connect(cache.path) as conn:
        conn.execute("UPDATE advice_review_plan SET hypothesis='changed without revision' WHERE id=?", (plan.id,))
    response = client.get("/api/reviews/due", params={"as_of": AS_OF, "page_size": 1})
    assert response.status_code == 409
    assert "完整性" in response.json()["detail"]


def test_valid_future_plan_beyond_calendar_horizon_does_not_block_already_due_queue(queue, monkeypatch):
    cache, client = queue
    monkeypatch.setattr(advice_review, "market_now_naive", lambda: datetime(2026, 12, 31, 16))
    old = [_create(cache) for _ in range(2)]
    first = _first(client)
    advice_id = _insert_advice(Path(cache.path), market_time="2026-12-30 15:15:00")
    payload = _plan_input(advice_id).model_copy(update={"horizon_days": 60})
    created = client.post("/api/reviews/plans", json=payload.model_dump(mode="json"))
    assert created.status_code == 201
    continuation = _next(client, first)
    assert continuation.status_code == 200
    assert continuation.json()["snapshot_token"] == first["snapshot_token"]
    december = _first(client, as_of="2026-12-31T16:00:00", page_size=200)
    assert [item["plan"]["id"] for item in december["items"]] == [plan.id for plan in old]
    assert december["total"] == 2


def test_cutoff_missing_from_queue_calendar_is_unavailable_instead_of_empty_complete(queue, monkeypatch):
    cache, client = queue
    _create(cache)
    sessions = tuple(day for day in bundled_exchange_sessions() if day.isoformat() != "2026-07-17")
    monkeypatch.setattr(advice_reviews, "bundled_exchange_sessions", lambda: sessions)
    response = client.get("/api/reviews/due", params={"as_of": AS_OF})
    assert response.status_code == 400
    assert "交易日历覆盖不足" in response.json()["detail"]
