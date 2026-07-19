from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_datahub
from app.api.routes import data
from app.services.trading_calendar import TradeCalendarRefreshResult


def test_refresh_trading_calendar_route_reports_success_without_error() -> None:
    client = _client()

    with patch(
        "app.api.routes.data.refresh_trade_calendar_result",
        return_value=TradeCalendarRefreshResult(trade_date_count=245, source="runtime_cache"),
    ):
        response = client.post("/api/data/trading-calendar/refresh")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "trade_date_count": 245,
        "source": "runtime_cache",
    }


def test_refresh_trading_calendar_route_reports_refresh_error() -> None:
    client = _client()

    with patch(
        "app.api.routes.data.refresh_trade_calendar_result",
        return_value=TradeCalendarRefreshResult(
            trade_date_count=0,
            source="bundled_baseline",
            error="ImportError: numpy.core.multiarray failed to import",
        ),
    ):
        response = client.post("/api/data/trading-calendar/refresh")

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "trade_date_count": 0,
        "source": "bundled_baseline",
        "error": "ImportError: numpy.core.multiarray failed to import",
    }


def test_refresh_trading_calendar_route_keeps_sync_refresh_off_event_loop() -> None:
    client = _client()
    offload = AsyncMock(return_value=TradeCalendarRefreshResult(245, "runtime_cache"))

    with patch("app.api.routes.data.asyncio.to_thread", offload):
        response = client.post("/api/data/trading-calendar/refresh")

    assert response.status_code == 200
    offload.assert_awaited_once_with(data.refresh_trade_calendar_result)


def test_refresh_trading_calendar_route_uses_refresh_response_model() -> None:
    app = FastAPI()
    app.include_router(data.router)

    schema = app.openapi()["paths"]["/api/data/trading-calendar/refresh"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    assert schema == {"$ref": "#/components/schemas/TradeCalendarRefreshResponse"}


def test_data_status_route_maps_sqlite_errors_to_api_detail() -> None:
    client = _client(_FailingDataHub(sqlite3.OperationalError("database is locked")))

    response = client.get("/api/data/status")

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}


def test_futu_status_route_maps_runtime_errors_to_api_detail() -> None:
    client = _client(_FailingDataHub(RuntimeError("Futu OpenD 未连接")))

    response = client.get("/api/futu/status")

    assert response.status_code == 503
    assert response.json() == {"detail": "Futu OpenD 未连接"}


def test_futu_status_route_returns_contract() -> None:
    client = _client(_FutuDataHub())

    response = client.get("/api/futu/status")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "Futu OpenD 可用", "latency_ms": 12.5}


def test_futu_status_route_uses_status_response_model() -> None:
    app = FastAPI()
    app.include_router(data.router)

    schema = app.openapi()["paths"]["/api/futu/status"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]

    assert schema == {"$ref": "#/components/schemas/FutuStatusResponse"}


def _client(datahub=None) -> TestClient:
    app = FastAPI()
    app.include_router(data.router)
    if datahub is not None:
        app.dependency_overrides[get_datahub] = lambda: datahub
    return TestClient(app)


class _FailingDataHub:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def status(self):
        raise self._exc

    async def futu_ping(self):
        raise self._exc


class _FutuDataHub:
    async def futu_ping(self):
        return {"ok": True, "message": "Futu OpenD 可用", "latency_ms": 12.5}
