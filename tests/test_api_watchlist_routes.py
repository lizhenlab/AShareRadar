from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_datahub
from app.api.routes import watchlist


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


class _DeleteCache:
    def __init__(self, *, removed: bool) -> None:
        self._removed = removed
        self.deleted_symbol = ""

    def delete_watchlist_item(self, symbol: str) -> bool:
        self.deleted_symbol = symbol
        return self._removed
