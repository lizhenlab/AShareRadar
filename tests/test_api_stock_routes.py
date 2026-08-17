from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import ResponseValidationError
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from app.api.deps import get_app_settings, get_datahub
from app.api.errors import (
    internal_validation_exception_handler,
    response_validation_exception_handler,
    sensitive_individual_exception_handler,
    sensitive_individual_http_exception_handler,
)
from app.api.routes import analysis, stock
from app.config import Settings
from app.services.individual_probability import individual_probability_store_for_cache_path
from tests.factories import make_kline, make_quote


def test_upside_probability_route_is_artifact_only_and_typed() -> None:
    datahub = _ProbabilityRouteHub(Path("data/ashare_radar.sqlite3"))
    app = FastAPI()
    app.include_router(stock.router)
    app.dependency_overrides[get_datahub] = lambda: datahub

    response = TestClient(app).get("/api/stock/upside-probability?symbol=600519")

    assert response.status_code == 200
    payload = response.json()
    assessment = individual_probability_store_for_cache_path(datahub.cache.path).latest()
    assert assessment is not None
    assert payload["symbol"] == "600519.SH"
    assert payload["status"] == "insufficient_data"
    assert payload["generated_at"] == assessment["generated_at"]
    assert response.headers["cache-control"] == "no-store"
    assert [(item["display_day"], item["holding_sessions"]) for item in payload["horizons"]] == [
        (2, 1),
        (3, 2),
        (4, 3),
    ]
    assert all(item["probability"] is None and item["confidence_interval"] is None for item in payload["horizons"])
    assert payload["horizons"][0]["calibration_metrics"]["actual_positive_rate_ci_95"] == {
        "lower": 0.3888845486111111,
        "upper": 0.5243098958333333,
        "level": 0.95,
    }
    assert datahub.provider_calls == 0


@pytest.mark.parametrize("symbol", ["600519.SZ", "000001.SH", "920066.SH"])
def test_upside_probability_route_rejects_code_exchange_mismatch(symbol: str) -> None:
    datahub = _ProbabilityRouteHub(Path("data/ashare_radar.sqlite3"))
    app = FastAPI()
    app.include_router(stock.router)
    app.dependency_overrides[get_datahub] = lambda: datahub

    response = TestClient(app).get(f"/api/stock/upside-probability?symbol={symbol}")

    assert response.status_code == 400
    assert response.json() == {"detail": "股票代码与 A 股交易所不一致"}
    assert datahub.provider_calls == 0


def test_upside_probability_route_maps_invalid_symbol_to_400() -> None:
    datahub = _ProbabilityRouteHub(Path("data/ashare_radar.sqlite3"))
    app = FastAPI()
    app.include_router(stock.router)
    app.dependency_overrides[get_datahub] = lambda: datahub

    response = TestClient(app).get("/api/stock/upside-probability?symbol=000000")

    assert response.status_code == 400
    assert response.json()["detail"] == "股票代码应为6位数字且不能全为0，例如 600519 或 000001"
    assert datahub.provider_calls == 0


def test_upside_probability_route_returns_409_for_corrupt_evidence(tmp_path: Path) -> None:
    directory = tmp_path / "research" / "individual_probability"
    directory.mkdir(parents=True)
    (directory / ("individual-upside-probability-assessment-" + "f" * 64 + ".json")).write_text(
        "{}",
        encoding="utf-8",
    )
    datahub = _ProbabilityRouteHub(tmp_path / "runtime.sqlite3")
    app = FastAPI()
    app.include_router(stock.router)
    app.dependency_overrides[get_datahub] = lambda: datahub

    response = TestClient(app).get("/api/stock/upside-probability?symbol=600519")

    assert response.status_code == 409
    assert response.json() == {"detail": "个股上涨概率证据损坏，拒绝回退旧版本"}


def test_upside_probability_invalid_symbol_precedes_corrupt_evidence(tmp_path: Path) -> None:
    directory = tmp_path / "research" / "individual_probability"
    directory.mkdir(parents=True)
    (directory / ("individual-upside-probability-assessment-" + "f" * 64 + ".json")).write_text(
        "{}",
        encoding="utf-8",
    )
    datahub = _ProbabilityRouteHub(tmp_path / "runtime.sqlite3")
    app = FastAPI()
    app.include_router(stock.router)
    app.dependency_overrides[get_datahub] = lambda: datahub

    response = TestClient(app).get("/api/stock/upside-probability?symbol=000000")

    assert response.status_code == 400
    assert response.json()["detail"] == "股票代码应为6位数字且不能全为0，例如 600519 或 000001"


def test_upside_probability_openapi_uses_typed_report() -> None:
    app = FastAPI()
    app.include_router(stock.router)

    schema = app.openapi()["paths"]["/api/stock/upside-probability"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]

    assert schema == {"$ref": "#/components/schemas/IndividualUpsideProbabilityReport"}


def test_upside_probability_unexpected_failure_is_generic_and_no_store(monkeypatch) -> None:
    datahub = _ProbabilityRouteHub(Path("data/ashare_radar.sqlite3"))
    app = FastAPI()
    app.include_router(stock.router)
    app.dependency_overrides[get_datahub] = lambda: datahub
    monkeypatch.setattr(
        stock,
        "individual_probability_store_for_cache_path",
        lambda _path: (_ for _ in ()).throw(TimeoutError("private probability path")),
    )

    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/stock/upside-probability?symbol=600519"
    )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "个股研究暂不可用，请稍后重试"}
    assert "private" not in response.text


def test_analyze_unexpected_failure_is_generic_and_no_store(monkeypatch) -> None:
    datahub = _ProbabilityRouteHub(Path("data/ashare_radar.sqlite3"))
    app = FastAPI()
    app.include_router(analysis.router)
    app.dependency_overrides[get_datahub] = lambda: datahub

    async def fail_analysis(*_args, **_kwargs):
        raise TimeoutError("primary timeout /private/analysis-secret.json")

    monkeypatch.setattr(analysis, "analyze_individual_stock", fail_analysis)

    response = TestClient(app, raise_server_exceptions=False).get("/api/analyze?symbol=600519")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "个股研究暂不可用，请稍后重试"}
    assert "private" not in response.text


def test_analyze_runtime_failure_is_redacted_and_no_store(monkeypatch) -> None:
    datahub = _ProbabilityRouteHub(Path("data/ashare_radar.sqlite3"))
    app = FastAPI()
    app.include_router(analysis.router)
    app.dependency_overrides[get_datahub] = lambda: datahub

    async def fail_analysis(*_args, **_kwargs):
        raise RuntimeError("internal /private/analysis-secret.json db=/tmp/private.sqlite3")

    monkeypatch.setattr(analysis, "analyze_individual_stock", fail_analysis)
    response = TestClient(app, raise_server_exceptions=False).get("/api/analyze?symbol=600519")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "个股研究暂不可用，请稍后重试"}
    assert "private" not in response.text


def test_analyze_response_validation_is_redacted_and_no_store(monkeypatch) -> None:
    datahub = _ProbabilityRouteHub(Path("data/ashare_radar.sqlite3"))
    app = FastAPI()
    app.add_exception_handler(ResponseValidationError, response_validation_exception_handler)
    app.include_router(analysis.router)
    app.dependency_overrides[get_datahub] = lambda: datahub

    async def invalid_analysis(*_args, **_kwargs):
        return {"private": "/private/response-secret.json"}

    monkeypatch.setattr(analysis, "analyze_individual_stock", invalid_analysis)
    response = TestClient(app, raise_server_exceptions=False).get("/api/analyze?symbol=600519")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "个股研究暂不可用，请稍后重试"}
    assert "private" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/analyze?symbol=600519",
        "/api/stock/workbench?symbol=600519",
        "/api/stock/upside-probability?symbol=600519",
    ],
)
def test_sensitive_individual_dependency_failure_is_redacted_and_no_store(path: str) -> None:
    app = FastAPI()
    app.add_exception_handler(Exception, sensitive_individual_exception_handler)
    app.include_router(analysis.router)
    app.include_router(stock.router)

    def fail_dependency():
        raise RuntimeError("private /tmp/secret api_key=abc")

    app.dependency_overrides[get_datahub] = fail_dependency
    response = TestClient(app, raise_server_exceptions=False).get(path)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "个股研究暂不可用，请稍后重试"}
    assert "private" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/analyze?symbol=600519",
        "/api/stock/workbench?symbol=600519",
        "/api/stock/upside-probability?symbol=600519",
    ],
)
def test_sensitive_dependency_http_5xx_is_redacted_and_no_store(path: str) -> None:
    app = FastAPI()
    app.add_exception_handler(HTTPException, sensitive_individual_http_exception_handler)
    app.add_exception_handler(Exception, sensitive_individual_exception_handler)
    app.include_router(analysis.router)
    app.include_router(stock.router)

    def fail_dependency():
        raise HTTPException(status_code=503, detail="private /tmp/provider-secret.json")

    app.dependency_overrides[get_datahub] = fail_dependency
    response = TestClient(app, raise_server_exceptions=False).get(path)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "个股研究暂不可用，请稍后重试"}
    assert "private" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/analyze?symbol=600519",
        "/api/stock/workbench?symbol=600519",
        "/api/stock/upside-probability?symbol=600519",
    ],
)
def test_sensitive_dependency_http_4xx_preserves_safe_detail_and_no_store(path: str) -> None:
    app = FastAPI()
    app.add_exception_handler(HTTPException, sensitive_individual_http_exception_handler)
    app.include_router(analysis.router)
    app.include_router(stock.router)

    def fail_dependency():
        raise HTTPException(
            status_code=409,
            detail="安全的业务冲突",
            headers={"X-Conflict-Reason": "safe"},
        )

    app.dependency_overrides[get_datahub] = fail_dependency
    response = TestClient(app).get(path)

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-conflict-reason"] == "safe"
    assert response.json() == {"detail": "安全的业务冲突"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/analyze?symbol=600519",
        "/api/stock/workbench?symbol=600519",
        "/api/stock/upside-probability?symbol=600519",
    ],
)
def test_sensitive_individual_dependency_validation_error_is_redacted_and_no_store(path: str) -> None:
    app = FastAPI()
    app.add_exception_handler(ValidationError, internal_validation_exception_handler)
    app.add_exception_handler(Exception, sensitive_individual_exception_handler)
    app.include_router(analysis.router)
    app.include_router(stock.router)

    def fail_dependency():
        TypeAdapter(int).validate_python("private-value")

    app.dependency_overrides[get_datahub] = fail_dependency
    response = TestClient(app, raise_server_exceptions=False).get(path)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "个股研究暂不可用，请稍后重试"}
    assert "private" not in response.text


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

    schema = app.openapi()["paths"]["/api/leaderboard"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]

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

    schema = app.openapi()["paths"]["/api/strong-stocks"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]

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


class _ProbabilityRouteCache:
    def __init__(self, path: Path) -> None:
        self.path = path


class _ProbabilityRouteHub:
    def __init__(self, path: Path) -> None:
        self.cache = _ProbabilityRouteCache(path)
        self.provider_calls = 0


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
