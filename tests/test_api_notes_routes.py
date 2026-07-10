from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.api.deps import get_datahub
from app.api.errors import validation_exception_handler
from app.api.routes import notes
from app.models.schemas import StockNoteInput
from app.services.cache import SQLiteCache
from tests.factories import make_quote


def test_create_stock_note_route_uses_quote_time_for_blank_trade_date() -> None:
    with TemporaryDirectory() as tmpdir:
        quote = make_quote(timestamp="2026-05-13 10:15:00")
        client = _client(_DataHubStub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3"), quote=quote))

        response = client.post(
            "/api/stock/notes",
            json={
                "symbol": "600519",
                "content": "  回踩观察  ",
                "trade_date": "   ",
                "price": 1288.0,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["content"] == "回踩观察"
    assert payload["trade_date"] == "2026-05-13 10:15:00"


def test_create_stock_note_route_rejects_non_positive_price() -> None:
    with TemporaryDirectory() as tmpdir:
        client = _client(_DataHubStub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3"), quote=make_quote()))

        response = client.post(
            "/api/stock/notes",
            json={
                "symbol": "600519",
                "content": "观察",
                "price": 0,
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "笔记价格必须大于0"}


def test_create_stock_note_route_rejects_non_finite_price() -> None:
    with TemporaryDirectory() as tmpdir:
        client = _client(_DataHubStub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3"), quote=make_quote()))

        response = client.post(
            "/api/stock/notes",
            json={
                "symbol": "600519",
                "content": "观察",
                "price": math.inf,
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "body / price: 应为有效数字"}


def test_create_stock_note_route_rejects_invalid_trade_date_and_normalizes_slashes() -> None:
    with TemporaryDirectory() as tmpdir:
        client = _client(_DataHubStub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3"), quote=make_quote()))

        invalid = client.post(
            "/api/stock/notes",
            json={
                "symbol": "600519",
                "content": "观察",
                "trade_date": "2026-02-31",
            },
        )
        normalized = client.post(
            "/api/stock/notes",
            json={
                "symbol": "600519",
                "content": "观察",
                "trade_date": "2026/05/13",
            },
        )

    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "笔记交易日期格式不合法"}
    assert normalized.status_code == 200
    assert normalized.json()["trade_date"] == "2026-05-13"


def test_update_stock_note_route_rejects_invalid_trade_date_and_normalizes_slashes() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        quote = make_quote()
        created = cache.create_stock_note(quote, StockNoteInput(symbol="600519", content="观察"))
        client = _client(_DataHubStub(cache=cache, quote=quote))

        invalid = client.patch(f"/api/stock/notes/{created.id}", json={"trade_date": "bad-date"})
        normalized = client.patch(f"/api/stock/notes/{created.id}", json={"trade_date": "2026/05/13"})

    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "笔记交易日期格式不合法"}
    assert normalized.status_code == 200
    assert normalized.json()["trade_date"] == "2026-05-13"


def test_update_stock_note_route_clears_blank_trade_date() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        quote = make_quote()
        created = cache.create_stock_note(
            quote,
            StockNoteInput(symbol="600519", content="观察", trade_date="2026-05-13 10:00:00"),
        )
        client = _client(_DataHubStub(cache=cache, quote=quote))

        response = client.patch(f"/api/stock/notes/{created.id}", json={"trade_date": "   "})

    assert response.status_code == 200
    assert response.json()["trade_date"] is None


def test_stock_notes_route_maps_sqlite_errors_to_api_detail() -> None:
    client = _client(_DataHubStub(cache=_FailingCache(sqlite3.OperationalError("database is locked")), quote=make_quote()))

    response = client.get("/api/stock/notes?symbol=600519")

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}


def test_delete_stock_note_returns_404_when_note_is_missing() -> None:
    client = _client(_DataHubStub(cache=_DeleteCache(removed=False), quote=make_quote()))

    response = client.delete("/api/stock/notes/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "个股笔记不存在"}


def test_delete_stock_note_returns_mutation_result_when_removed() -> None:
    client = _client(_DataHubStub(cache=_DeleteCache(removed=True), quote=make_quote()))

    response = client.delete("/api/stock/notes/9")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "removed": True}


def test_delete_stock_note_uses_mutation_response_model() -> None:
    app = FastAPI()
    app.include_router(notes.router)

    schema = app.openapi()["paths"]["/api/stock/notes/{note_id}"]["delete"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    assert schema == {"$ref": "#/components/schemas/MutationResult"}


def _client(datahub) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(notes.router)
    app.dependency_overrides[get_datahub] = lambda: datahub
    return TestClient(app)


class _DataHubStub:
    def __init__(self, *, cache, quote) -> None:
        self.cache = cache
        self._quote = quote

    async def quote(self, symbol: str):
        return self._quote


class _FailingCache:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def stock_notes(self, symbol: str, limit: int = 100):
        raise self._exc


class _DeleteCache:
    def __init__(self, *, removed: bool) -> None:
        self._removed = removed

    def delete_stock_note(self, note_id: int) -> bool:
        return self._removed
