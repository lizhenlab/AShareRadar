from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_app_settings, get_datahub
from app.api.routes import analysis, stock
from app.config import Settings
from tests.factories import make_kline, make_quote


def test_strong_stocks_route_returns_contract_for_custom_symbols() -> None:
    datahub = _StrongStocksSuccessHub()
    app = FastAPI()
    app.include_router(analysis.router)
    app.dependency_overrides[get_datahub] = lambda: datahub
    app.dependency_overrides[get_app_settings] = lambda: Settings()

    response = TestClient(app).get("/api/strong-stocks?symbols=600519.SH")

    assert response.status_code == 200
    payload = response.json()
    assert payload["updated_at"] == "2026-05-13 10:00:00"
    assert payload["scope"] == "自定义列表"
    assert payload["sample_count"] == 1
    assert payload["requested_count"] == 1
    assert payload["missing_count"] == 0
    assert payload["degraded"] is False
    assert payload["warnings"] == []
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["rank"] == 1
    assert item["code"] == "600519"
    assert item["name"] == "贵州茅台"
    assert item["price"] == 1300.0
    assert item["change_pct"] == 0.78
    assert isinstance(item["trend_score"], int)
    assert isinstance(item["leader_score"], int)
    assert isinstance(item["reason"], str)
    assert isinstance(item["tags"], list)
    assert datahub.quote_calls == [["600519.SH"]]
    assert datahub.kline_calls == [("600519.SH", 80)]


def test_leaderboard_route_uses_strong_stock_response_model() -> None:
    app = FastAPI()
    app.include_router(analysis.router)

    schema = app.openapi()["paths"]["/api/leaderboard"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]

    assert schema == {"$ref": "#/components/schemas/StrongStockWatchResponse"}


def test_strong_stocks_route_reports_custom_quote_source_unavailable() -> None:
    datahub = _StrongStocksRouteHub()
    app = FastAPI()
    app.include_router(analysis.router)
    app.dependency_overrides[get_datahub] = lambda: datahub
    app.dependency_overrides[get_app_settings] = lambda: Settings()

    response = TestClient(app).get("/api/strong-stocks?symbols=600001.SH,600002.SH")

    assert response.status_code == 503
    assert response.json()["detail"] == "自定义强股列表行情不可用：600001.SH、600002.SH"
    assert datahub.quote_calls == [["600001.SH", "600002.SH"], ["600001.SH"], ["600002.SH"]]


def test_strong_stocks_route_uses_strong_stock_response_model() -> None:
    app = FastAPI()
    app.include_router(analysis.router)

    schema = app.openapi()["paths"]["/api/strong-stocks"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]

    assert schema == {"$ref": "#/components/schemas/StrongStockWatchResponse"}


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


class _RouteCache:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def log_event(self, category: str, message: str) -> None:
        self.events.append((category, message))


class _StrongStocksSuccessHub:
    def __init__(self) -> None:
        self.cache = _RouteCache()
        self.quote_calls: list[list[str]] = []
        self.kline_calls: list[tuple[str, int]] = []

    async def quotes(self, symbols, use_cache: bool = True):
        normalized = list(symbols)
        self.quote_calls.append(normalized)
        return [make_quote()]

    async def kline(self, symbol: str, limit: int = 80):
        self.kline_calls.append((symbol, limit))
        return [make_kline(close=120 + index, date=f"2026-04-{index + 1:02d}") for index in range(25)]


class _StrongStocksRouteHub:
    def __init__(self) -> None:
        self.cache = _RouteCache()
        self.quote_calls: list[list[str]] = []

    async def quotes(self, symbols, use_cache: bool = True):
        normalized = list(symbols)
        self.quote_calls.append(normalized)
        raise RuntimeError("quotes down")

    async def kline(self, symbol: str, limit: int = 80):
        raise AssertionError("K-line should not be fetched when no quotes are available")
