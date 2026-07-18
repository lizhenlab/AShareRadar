from __future__ import annotations

import asyncio
import sqlite3
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_datahub
from app.api.routes import watchlist


def test_watchlist_route_runs_cache_read_off_event_loop_thread() -> None:
    cache = _ThreadTrackingCache()

    async def invoke() -> tuple[list[object], int]:
        event_loop_thread_id = threading.get_ident()
        result = await watchlist.watchlist(_DataHubStub(cache=cache))
        return result, event_loop_thread_id

    result, event_loop_thread_id = asyncio.run(invoke())

    assert result == []
    assert cache.call_thread_id is not None
    assert cache.call_thread_id != event_loop_thread_id


def test_watchlist_route_maps_sqlite_errors_to_api_detail() -> None:
    client = _client(_DataHubStub(cache=_FailingCache(sqlite3.OperationalError("database is locked"))))

    response = client.get("/api/watchlist")

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}


def test_advice_history_route_maps_sqlite_errors_to_api_detail() -> None:
    client = _client(_DataHubStub(cache=_FailingCache(sqlite3.OperationalError("database is locked"))))

    response = client.get("/api/advice/history?symbol=600519")

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}


def test_advice_timeline_route_maps_sqlite_errors_to_api_detail() -> None:
    client = _client(_DataHubStub(cache=_FailingCache(sqlite3.OperationalError("database is locked"))))

    response = client.get("/api/advice/timeline?symbol=600519")

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}


def test_advice_timeline_route_validates_symbol_and_limit() -> None:
    cache = _TimelineCache()
    client = _client(_DataHubStub(cache=cache))

    invalid_symbol = client.get("/api/advice/timeline?symbol=invalid&limit=8")
    invalid_limits = [
        client.get("/api/advice/timeline?symbol=600519&limit=0"),
        client.get("/api/advice/timeline?symbol=600519&limit=201"),
        client.get("/api/advice/timeline?symbol=600519&limit=bad"),
    ]

    assert invalid_symbol.status_code == 400
    assert all(response.status_code == 422 for response in invalid_limits)
    assert cache.calls == []


def test_advice_timeline_route_passes_one_symbol_and_limit_to_cache() -> None:
    cache = _TimelineCache()
    client = _client(_DataHubStub(cache=cache))

    response = client.get("/api/advice/timeline?symbol=600519&limit=8")

    assert response.status_code == 200
    assert response.json() == []
    assert cache.calls == [("600519", 8)]


def test_advice_timeline_route_uses_timeline_response_model() -> None:
    app = FastAPI()
    app.include_router(watchlist.router)

    schema = app.openapi()["paths"]["/api/advice/timeline"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    assert schema["type"] == "array"
    assert schema["items"] == {"$ref": "#/components/schemas/AdviceTimelineItem"}


def test_delete_watchlist_item_returns_404_when_symbol_is_missing() -> None:
    cache = _DeleteCache(removed=False)
    client = _client(_DataHubStub(cache=cache))

    response = client.delete("/api/watchlist/600519")

    assert response.status_code == 404
    assert response.json() == {"detail": "自选股不存在"}
    assert cache.deleted_symbol == "600519"


def test_delete_watchlist_item_returns_mutation_result_when_removed() -> None:
    cache = _DeleteCache(removed=True)
    client = _client(_DataHubStub(cache=cache))

    response = client.delete("/api/watchlist/600519")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "removed": True}
    assert cache.deleted_symbol == "600519"


def test_delete_watchlist_item_uses_mutation_response_model() -> None:
    app = FastAPI()
    app.include_router(watchlist.router)

    schema = app.openapi()["paths"]["/api/watchlist/{symbol}"]["delete"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    assert schema == {"$ref": "#/components/schemas/MutationResult"}


def _client(datahub) -> TestClient:
    app = FastAPI()
    app.include_router(watchlist.router)
    app.dependency_overrides[get_datahub] = lambda: datahub
    return TestClient(app)


class _DataHubStub:
    def __init__(self, *, cache) -> None:
        self.cache = cache


class _FailingCache:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def watchlist(self):
        raise self._exc

    def advice_history(self, symbol: str, limit: int = 30):
        raise self._exc

    def advice_timeline(self, symbol: str, limit: int = 30):
        raise self._exc


class _ThreadTrackingCache:
    def __init__(self) -> None:
        self.call_thread_id: int | None = None

    def watchlist(self) -> list[object]:
        self.call_thread_id = threading.get_ident()
        return []


class _DeleteCache:
    def __init__(self, *, removed: bool) -> None:
        self._removed = removed
        self.deleted_symbol = ""

    def delete_watchlist_item(self, symbol: str) -> bool:
        self.deleted_symbol = symbol
        return self._removed


class _TimelineCache:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def advice_timeline(self, symbol: str, limit: int = 30) -> list[object]:
        self.calls.append((symbol, limit))
        return []
