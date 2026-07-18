from __future__ import annotations

import asyncio
from datetime import date
import json
import math
import sqlite3
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
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
    stream_quotes,
)
from app.models.market import Kline, MinuteKline, OrderBookLevel, PlateItem, Quote, StockConceptItem
from tests.factories import make_kline, make_plate_item, make_quote


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


def test_stream_empty_fallback_uses_active_selection_and_ignores_seeds() -> None:
    selection = SimpleNamespace(
        active_symbols=("300750", "300750.SZ", "bad"),
        excluded_symbols=("600519",),
        has_entries=True,
    )
    hub = _Hub(watchlist_selection=selection)
    settings = SimpleNamespace(seed_symbols=("600519", "000001"))

    assert _stream_symbol_list("", hub, settings) == ["300750.SZ"]


def test_stream_empty_fallback_uses_seeds_only_for_truly_empty_selection() -> None:
    selection = SimpleNamespace(active_symbols=(), excluded_symbols=(), has_entries=False)
    hub = _Hub(watchlist_selection=selection)
    settings = SimpleNamespace(seed_symbols=("600519", "600519.SH", "bad", "000001"))

    assert _stream_symbol_list("", hub, settings) == ["600519.SH", "000001.SZ"]


def test_stream_all_excluded_fallback_raises_but_explicit_excluded_query_is_allowed() -> None:
    selection = SimpleNamespace(
        active_symbols=(),
        excluded_symbols=("600519", "600519.SH"),
        has_entries=True,
    )
    hub = _Hub(watchlist_selection=selection)
    settings = SimpleNamespace(seed_symbols=("600519", "000001"))

    with pytest.raises(ValueError, match="观察池没有可订阅的活跃股票"):
        _stream_symbol_list("", hub, settings)

    hub.cache.call_thread_id = None
    assert _stream_symbol_list("600519", hub, settings) == ["600519.SH"]
    assert hub.cache.call_thread_id is None


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


def test_quote_stream_route_maps_requested_symbol_errors_without_reading_watchlist() -> None:
    hub = _Hub()
    app = FastAPI()
    app.include_router(quote_routes.router)
    app.dependency_overrides[get_datahub] = lambda: hub
    app.dependency_overrides[get_app_settings] = lambda: SimpleNamespace(
        seed_symbols=("600519",),
        quote_refresh_seconds=1,
    )

    response = TestClient(app).get("/api/stream/quotes?symbols=not-a-code")

    assert response.status_code == 400
    assert response.json() == {"detail": "股票代码应为6位数字且不能全为0，例如 600519 或 000001"}
    assert hub.cache.call_thread_id is None


def test_quote_stream_route_returns_422_when_watchlist_is_all_excluded() -> None:
    selection = SimpleNamespace(
        active_symbols=(),
        excluded_symbols=("600519",),
        has_entries=True,
    )
    hub = _Hub(watchlist_selection=selection)
    app = FastAPI()
    app.include_router(quote_routes.router)
    app.dependency_overrides[get_datahub] = lambda: hub
    app.dependency_overrides[get_app_settings] = lambda: SimpleNamespace(
        seed_symbols=("600519", "000001"),
        quote_refresh_seconds=1,
    )

    response = TestClient(app).get("/api/stream/quotes?symbols=,,")

    assert response.status_code == 422
    assert response.json() == {"detail": "观察池没有可订阅的活跃股票，请显式输入股票代码"}
    assert hub.requested_symbols == []


def test_quote_stream_watchlist_fallback_runs_off_event_loop_thread() -> None:
    hub = _Hub(watchlist_symbols=["300750"])

    async def invoke() -> tuple[StreamingResponse, int]:
        event_loop_thread_id = threading.get_ident()
        response = await stream_quotes(
            request=_Request(disconnected=[True]),
            symbols="",
            datahub=hub,
            settings=SimpleNamespace(seed_symbols=("600519",), quote_refresh_seconds=1),
        )
        return response, event_loop_thread_id

    response, event_loop_thread_id = asyncio.run(invoke())

    assert response.media_type == "text/event-stream"
    assert hub.cache.call_thread_id is not None
    assert hub.cache.call_thread_id != event_loop_thread_id


def test_quote_stream_contract_declares_event_stream_and_disables_buffering() -> None:
    app = FastAPI()
    app.include_router(quote_routes.router)

    content = app.openapi()["paths"]["/api/stream/quotes"]["get"]["responses"]["200"]["content"]
    assert content == {"text/event-stream": {"schema": {"type": "string"}}}

    response = asyncio.run(
        stream_quotes(
            request=_Request(disconnected=[True]),
            symbols="600519",
            datahub=_Hub(quotes=[make_quote()]),
            settings=SimpleNamespace(seed_symbols=("600519",), quote_refresh_seconds=1),
        )
    )
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_required_market_numbers_reject_non_finite_values(value: float) -> None:
    cases = [
        (Quote, {**make_quote().model_dump(), "price": value}),
        (Kline, {**make_kline().model_dump(), "high": value}),
        (
            MinuteKline,
            {
                "timestamp": "2026-07-14 10:00:00",
                "open": 10,
                "close": 10,
                "high": 11,
                "low": 9,
                "volume": value,
            },
        ),
        (PlateItem, {**make_plate_item().model_dump(), "change_pct": value}),
        (
            StockConceptItem,
            {
                "symbol": "600519.SH",
                "rank": 1,
                "name": "白酒",
                "change_pct": value,
                "source": "测试",
                "updated_at": "2026-07-14 10:00:00",
            },
        ),
        (OrderBookLevel, {"price": value, "volume": 100}),
    ]

    for model, payload in cases:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_nullable_market_numbers_accept_none_but_not_non_finite_values() -> None:
    payload = {**make_quote().model_dump(), "pe": None, "pb": None, "market_cap": None}
    assert Quote.model_validate(payload).pe is None

    with pytest.raises(ValidationError):
        Quote.model_validate({**payload, "pe": math.inf})


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


def test_next_quote_stream_event_redacts_provider_credentials() -> None:
    event = asyncio.run(
        _next_quote_stream_event(
            _Hub(error=RuntimeError("source down https://example.test/quote?api_key=secret-key")),
            ["600519.SH"],
        )
    )

    assert "secret-key" not in event
    assert "api_key=<redacted>" in event


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
        watchlist_selection=None,
        quotes=None,
        error: Exception | None = None,
        cache_error: Exception | None = None,
    ) -> None:
        self.cache = (
            _SelectionCache(watchlist_selection, error=cache_error)
            if watchlist_selection is not None
            else _Cache(watchlist_symbols or [], error=cache_error)
        )
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
        self.call_thread_id: int | None = None

    def watchlist_symbols(self) -> list[str]:
        self.call_thread_id = threading.get_ident()
        if self._error:
            raise self._error
        return self._symbols


class _SelectionCache(_Cache):
    def __init__(self, selection, *, error: Exception | None = None) -> None:
        super().__init__([], error=error)
        self._selection = selection

    def watchlist_symbol_selection(self):
        self.call_thread_id = threading.get_ident()
        if self._error:
            raise self._error
        return self._selection


class _Request:
    def __init__(self, *, disconnected: list[bool]) -> None:
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected.pop(0) if self._disconnected else True
