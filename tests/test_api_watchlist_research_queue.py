from __future__ import annotations

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_datahub
from app.api.routes import watchlist
from app.models.schemas import WatchlistItem
from tests.factories import make_quote


def test_patch_watchlist_updates_only_present_metadata_without_calling_provider() -> None:
    cache = _QueueCache()
    hub = _NoProviderHub(cache)
    client = _client(hub)

    response = client.patch(
        "/api/watchlist/600519",
        json={"priority": "high", "note": None, "next_review_date": None},
    )

    assert response.status_code == 200
    assert cache.updated_symbol == "600519"
    assert cache.updated_fields == {"priority", "note", "next_review_date"}
    assert cache.updated_payload.note is None
    assert cache.updated_payload.next_review_date is None
    assert hub.quote_calls == 0


def test_mark_viewed_route_can_preserve_unread_count_without_calling_provider() -> None:
    cache = _QueueCache()
    hub = _NoProviderHub(cache)
    client = _client(hub)

    response = client.post(
        "/api/watchlist/600519/mark-viewed",
        json={"clear_unread": False},
    )

    assert response.status_code == 200
    assert cache.marked == ("600519", False, None)
    assert hub.quote_calls == 0


def test_mark_viewed_route_passes_displayed_advice_watermark() -> None:
    cache = _QueueCache()
    hub = _NoProviderHub(cache)
    client = _client(hub)

    response = client.post(
        "/api/watchlist/600519/mark-viewed",
        json={"clear_unread": True, "viewed_through_advice_id": 42},
    )

    assert response.status_code == 200
    assert cache.marked == ("600519", True, 42)
    assert hub.quote_calls == 0


@pytest.mark.parametrize("watermark", [0, -1, True, 1.5, "42"])
def test_mark_viewed_route_rejects_invalid_advice_watermark(watermark: object) -> None:
    cache = _QueueCache()
    client = _client(_NoProviderHub(cache))

    response = client.post(
        "/api/watchlist/600519/mark-viewed",
        json={"clear_unread": True, "viewed_through_advice_id": watermark},
    )

    assert response.status_code == 422
    assert cache.marked is None


def test_patch_and_mark_viewed_return_404_for_unknown_symbol() -> None:
    cache = _QueueCache(missing=True)
    client = _client(_NoProviderHub(cache))

    patch_response = client.patch("/api/watchlist/000001", json={"priority": "low"})
    viewed_response = client.post("/api/watchlist/000001/mark-viewed", json={"clear_unread": True})

    assert patch_response.status_code == 404
    assert patch_response.json() == {"detail": "自选股不存在"}
    assert viewed_response.status_code == 404
    assert viewed_response.json() == {"detail": "自选股不存在"}


@pytest.mark.parametrize(
    "payload",
    [
        {"research_status": "pending"},
        {"priority": "urgent"},
        {"next_review_date": "2026/07/15"},
        {"next_review_date": "2026-02-30"},
        {"unread_change_count": -1},
        {"note": "x" * 81},
        {"group_name": "x" * 21},
        {"name": "不允许更改身份字段"},
    ],
)
def test_patch_watchlist_rejects_invalid_metadata_with_422(payload: dict[str, object]) -> None:
    cache = _QueueCache()
    client = _client(_NoProviderHub(cache))

    response = client.patch("/api/watchlist/600519", json=payload)

    assert response.status_code == 422
    assert cache.updated_payload is None


def test_legacy_post_payload_remains_compatible_and_new_fields_are_optional() -> None:
    cache = _PostCache()
    hub = _PostHub(cache)
    client = _client(hub)

    response = client.post(
        "/api/watchlist",
        json={"symbol": "600519", "note": "老版本关注理由", "group_name": "白酒", "pinned": True},
    )

    assert response.status_code == 200
    assert hub.requested_symbol == "600519"
    assert cache.saved_kwargs == {
        "note": "老版本关注理由",
        "group_name": "白酒",
        "pinned": True,
        "research_status": None,
        "priority": None,
        "next_review_date": None,
    }


def test_post_accepts_initial_queue_metadata() -> None:
    cache = _PostCache()
    client = _client(_PostHub(cache))

    response = client.post(
        "/api/watchlist",
        json={
            "symbol": "600519",
            "research_status": "to_research",
            "priority": "high",
            "next_review_date": "2026-07-20",
        },
    )

    assert response.status_code == 200
    assert cache.saved_kwargs["research_status"] == "to_research"
    assert cache.saved_kwargs["priority"] == "high"
    assert cache.saved_kwargs["next_review_date"] == date(2026, 7, 20)


def _client(datahub) -> TestClient:
    app = FastAPI()
    app.include_router(watchlist.router)
    app.dependency_overrides[get_datahub] = lambda: datahub
    return TestClient(app)


def _item(**updates) -> WatchlistItem:
    values = {
        "symbol": "600519.SH",
        "code": "600519",
        "market": "SH",
        "name": "贵州茅台",
        "created_at": "2026-07-15 09:00:00",
        "updated_at": "2026-07-15 10:00:00",
    }
    values.update(updates)
    return WatchlistItem(**values)


class _NoProviderHub:
    def __init__(self, cache) -> None:
        self.cache = cache
        self.quote_calls = 0

    async def quote(self, symbol: str):
        self.quote_calls += 1
        raise AssertionError(f"PATCH route unexpectedly requested quote for {symbol}")


class _QueueCache:
    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing
        self.updated_symbol = ""
        self.updated_payload = None
        self.updated_fields: set[str] = set()
        self.marked: tuple[str, bool, int | None] | None = None

    def update_watchlist_item(self, symbol: str, payload):
        self.updated_symbol = symbol
        self.updated_payload = payload
        self.updated_fields = set(payload.model_fields_set)
        return None if self.missing else _item(priority=payload.priority)

    def mark_watchlist_viewed(
        self,
        symbol: str,
        *,
        clear_unread: bool = True,
        viewed_through_advice_id: int | None = None,
    ):
        self.marked = (symbol, clear_unread, viewed_through_advice_id)
        return None if self.missing else _item(last_viewed_at="2026-07-15 12:00:00", unread_change_count=2)


class _PostHub:
    def __init__(self, cache) -> None:
        self.cache = cache
        self.requested_symbol = ""

    async def quote(self, symbol: str):
        self.requested_symbol = symbol
        return make_quote()


class _PostCache:
    def __init__(self) -> None:
        self.saved_kwargs: dict[str, object] = {}

    def save_watchlist_item(self, quote, **kwargs):
        self.saved_kwargs = kwargs
        return _item(name=quote.name)
