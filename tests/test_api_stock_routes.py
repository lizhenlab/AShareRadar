from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_datahub
from app.api.routes import stock


def test_minute_analysis_route_rejects_zero_symbol_before_fetching_data() -> None:
    datahub = _MinuteRouteHub()
    app = FastAPI()
    app.include_router(stock.router)
    app.dependency_overrides[get_datahub] = lambda: datahub

    response = TestClient(app).get("/api/stock/minute-analysis?symbol=000000&interval=5m&limit=120")

    assert response.status_code == 400
    assert response.json() == {"detail": "股票代码应为6位数字且不能全为0，例如 600519 或 000001"}
    assert datahub.profile_calls == []
    assert datahub.minute_calls == []


class _MinuteRouteHub:
    def __init__(self) -> None:
        self.profile_calls: list[str] = []
        self.minute_calls: list[tuple[str, str, int]] = []

    async def stock_profile(self, symbol: str):
        self.profile_calls.append(symbol)
        return None

    async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120):
        self.minute_calls.append((symbol, interval, limit))
        raise AssertionError("minute data should not be fetched for unknown symbols")
