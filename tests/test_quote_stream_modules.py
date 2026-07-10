from __future__ import annotations

import asyncio
from datetime import date
import json
import math
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_app_settings, get_datahub
from app.api.routes import quotes as quote_routes
from app.api.routes.quotes import (
    MAX_QUOTE_SYMBOLS,
    MIN_QUOTE_REFRESH_SECONDS,
    _next_quote_stream_event,
    _quote_stream_events,
    _quote_refresh_interval,
    _quote_symbol_list,
    _sse_message,
    _stream_symbol_list,
)
from tests.factories import make_quote


def test_stream_symbol_list_uses_requested_symbols_before_fallbacks() -> None:
    hub = _Hub(watchlist_symbols=["300750"])
    settings = SimpleNamespace(seed_symbols=("600000", "000001", "600519"))

    symbols = _stream_symbol_list(" 600519 , 600519.SH , 000001.SZ ", hub, settings)

    assert symbols == ["600519.SH", "000001.SZ"]


def test_stream_symbol_list_uses_watchlist_then_seed_symbols() -> None:
    settings = SimpleNamespace(seed_symbols=("600000", "000001", "600519", "300750", "601318", "000858"))

    assert _stream_symbol_list(" ,, ", _Hub(watchlist_symbols=["300750"]), settings) == ["300750.SZ"]
    assert _stream_symbol_list("", _Hub(watchlist_symbols=[]), settings) == [
        "600000.SH",
        "000001.SZ",
        "600519.SH",
        "300750.SZ",
        "601318.SH",
    ]


def test_stream_symbol_list_skips_dirty_fallback_symbols() -> None:
    settings = SimpleNamespace(seed_symbols=("600000", "not-a-code", "000001"))

    assert _stream_symbol_list(
        "",
        _Hub(watchlist_symbols=["not-a-code", "300750", "000000", "300750.SZ"]),
        settings,
    ) == ["300750.SZ"]
    assert _stream_symbol_list("", _Hub(watchlist_symbols=["not-a-code", "000000"]), settings) == [
        "600000.SH",
        "000001.SZ",
    ]


def test_stream_symbol_list_caps_watchlist_fallback_without_rejecting() -> None:
    settings = SimpleNamespace(seed_symbols=("600519",))
    watchlist = [f"60{index:04d}" for index in range(MAX_QUOTE_SYMBOLS + 5)]

    symbols = _stream_symbol_list("", _Hub(watchlist_symbols=watchlist), settings)

    assert len(symbols) == MAX_QUOTE_SYMBOLS
    assert symbols[0] == "600000.SH"
    assert symbols[-1] == f"60{MAX_QUOTE_SYMBOLS - 1:04d}.SH"


def test_stream_symbol_list_rejects_invalid_symbols() -> None:
    with pytest.raises(ValueError):
        _stream_symbol_list("not-a-code", _Hub(), SimpleNamespace(seed_symbols=("600519",)))


def test_quote_symbol_list_requires_at_least_one_symbol() -> None:
    assert _quote_symbol_list(" 600519 , 000001.SZ ") == ["600519.SH", "000001.SZ"]
    with pytest.raises(ValueError, match="至少输入一个股票代码"):
        _quote_symbol_list(" ,, ")


def test_quote_symbol_list_limits_unique_batch_size() -> None:
    symbols = ",".join(f"60{index:04d}" for index in range(MAX_QUOTE_SYMBOLS + 1))

    with pytest.raises(ValueError, match=f"一次最多查询 {MAX_QUOTE_SYMBOLS} 个股票代码"):
        _quote_symbol_list(symbols)

    repeated = ",".join(["600519"] * (MAX_QUOTE_SYMBOLS + 5))
    assert _quote_symbol_list(repeated) == ["600519.SH"]


def test_quotes_route_dedupes_normalized_symbols_before_fetching_datahub() -> None:
    hub = _Hub(quotes=[make_quote()])
    app = FastAPI()
    app.include_router(quote_routes.router)
    app.dependency_overrides[get_datahub] = lambda: hub

    response = TestClient(app).get("/api/quotes?symbols=600519,600519.SH,000001")

    assert response.status_code == 200
    assert hub.requested_symbols == ["600519.SH", "000001.SZ"]


def test_quotes_route_rejects_empty_symbols_before_fetching_datahub() -> None:
    hub = _Hub(quotes=[make_quote()])
    app = FastAPI()
    app.include_router(quote_routes.router)
    app.dependency_overrides[get_datahub] = lambda: hub

    response = TestClient(app).get("/api/quotes?symbols=,,")

    assert response.status_code == 400
    assert response.json() == {"detail": "至少输入一个股票代码"}
    assert hub.requested_symbols == []


def test_quotes_route_rejects_oversized_symbol_batch_before_fetching_datahub() -> None:
    hub = _Hub(quotes=[make_quote()])
    app = FastAPI()
    app.include_router(quote_routes.router)
    app.dependency_overrides[get_datahub] = lambda: hub
    symbols = ",".join(f"60{index:04d}" for index in range(MAX_QUOTE_SYMBOLS + 1))

    response = TestClient(app).get(f"/api/quotes?symbols={symbols}")

    assert response.status_code == 400
    assert response.json() == {"detail": f"一次最多查询 {MAX_QUOTE_SYMBOLS} 个股票代码"}
    assert hub.requested_symbols == []


def test_quote_stream_route_maps_watchlist_sqlite_errors_to_api_detail() -> None:
    hub = _Hub(cache_error=sqlite3.OperationalError("database is locked"))
    app = FastAPI()
    app.include_router(quote_routes.router)
    app.dependency_overrides[get_datahub] = lambda: hub
    app.dependency_overrides[get_app_settings] = lambda: SimpleNamespace(
        seed_symbols=("600519",),
        quote_refresh_seconds=1,
    )

    response = TestClient(app).get("/api/stream/quotes?symbols=,,")

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}
    assert hub.requested_symbols == []


def test_sse_message_formats_named_and_default_events() -> None:
    assert _sse_message([{"code": "600519"}]) == 'data: [{"code": "600519"}]\n\n'
    assert _sse_message({"message": "错误"}, event="quote-error") == 'event: quote-error\ndata: {"message": "错误"}\n\n'
    assert _sse_message({"date": date(2026, 5, 13)}) == 'data: {"date": "2026-05-13"}\n\n'
    assert json.loads(_sse_message({"price": math.nan, "items": [math.inf, -math.inf]}).removeprefix("data: ")) == {
        "price": None,
        "items": [None, None],
    }


def test_quote_refresh_interval_has_safe_lower_bound() -> None:
    assert _quote_refresh_interval(3) == 3
    assert _quote_refresh_interval(0) == MIN_QUOTE_REFRESH_SECONDS
    assert _quote_refresh_interval(-5) == MIN_QUOTE_REFRESH_SECONDS
    assert _quote_refresh_interval(math.inf) == MIN_QUOTE_REFRESH_SECONDS
    assert _quote_refresh_interval(math.nan) == MIN_QUOTE_REFRESH_SECONDS
    assert _quote_refresh_interval("bad") == MIN_QUOTE_REFRESH_SECONDS


def test_next_quote_stream_event_serializes_quotes_and_errors() -> None:
    async def run_check() -> tuple[str, str, str, list[str]]:
        success_hub = _Hub(quotes=[make_quote()])
        success = await _next_quote_stream_event(success_hub, ["600519.SH"])
        error = await _next_quote_stream_event(_Hub(error=RuntimeError("source down")), ["600519.SH"])
        empty_error = await _next_quote_stream_event(_Hub(error=TimeoutError()), ["600519.SH"])
        return success, error, empty_error, success_hub.requested_symbols

    success, error, empty_error, requested_symbols = asyncio.run(run_check())

    assert requested_symbols == ["600519.SH"]
    assert success.startswith("data: ")
    assert json.loads(success.removeprefix("data: ").strip()) == [make_quote().model_dump()]
    assert error == 'event: quote-error\ndata: {"message": "source down"}\n\n'
    assert empty_error == 'event: quote-error\ndata: {"message": "TimeoutError: 数据源响应超时"}\n\n'


def test_quote_stream_events_stop_after_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(quote_routes.asyncio, "sleep", fake_sleep)

    async def run_check() -> str:
        generator = _quote_stream_events(_Request(disconnected=[False, True]), _Hub(quotes=[make_quote()]), ["600519.SH"], 0)
        first = await anext(generator)
        with pytest.raises(StopAsyncIteration):
            await anext(generator)
        return first

    event = asyncio.run(run_check())

    assert event.startswith("data: ")
    assert sleeps == [MIN_QUOTE_REFRESH_SECONDS]


class _Hub:
    def __init__(
        self,
        *,
        watchlist_symbols: list[str] | None = None,
        quotes=None,
        error: Exception | None = None,
        cache_error: Exception | None = None,
    ) -> None:
        self.cache = _Cache(watchlist_symbols or [], error=cache_error)
        self._quotes = quotes or []
        self._error = error
        self.requested_symbols: list[str] = []

    async def quotes(self, symbols: list[str]):
        self.requested_symbols = symbols
        if self._error:
            raise self._error
        return self._quotes


class _Cache:
    def __init__(self, symbols: list[str], *, error: Exception | None = None) -> None:
        self._symbols = symbols
        self._error = error

    def watchlist_symbols(self) -> list[str]:
        if self._error:
            raise self._error
        return self._symbols


class _Request:
    def __init__(self, *, disconnected: list[bool]) -> None:
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected.pop(0) if self._disconnected else True
