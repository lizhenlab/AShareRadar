from __future__ import annotations

import sqlite3
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_datahub
from app.api.routes import data
from app.services.trading_calendar import TradeCalendarRefreshResult


def test_refresh_trading_calendar_route_reports_success_without_error() -> None:
    client = _client()

    with patch(
        "app.api.routes.data.refresh_trade_calendar_result",
        return_value=TradeCalendarRefreshResult(trade_date_count=245, source="交易日历缓存"),
    ):
        response = client.post("/api/data/trading-calendar/refresh")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "trade_date_count": 245,
        "source": "交易日历缓存",
    }


def test_refresh_trading_calendar_route_reports_refresh_error() -> None:
    client = _client()

    with patch(
        "app.api.routes.data.refresh_trade_calendar_result",
        return_value=TradeCalendarRefreshResult(
            trade_date_count=0,
            source="工作日兜底",
            error="ImportError: numpy.core.multiarray failed to import",
        ),
    ):
        response = client.post("/api/data/trading-calendar/refresh")

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "trade_date_count": 0,
        "source": "工作日兜底",
        "error": "ImportError: numpy.core.multiarray failed to import",
    }


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
