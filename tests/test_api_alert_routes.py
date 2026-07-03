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
from app.api.routes import alerts
from app.models.schemas import AlertRuleInput
from app.services.cache import SQLiteCache
from tests.factories import make_quote


def test_create_alert_route_uses_default_name_for_blank_name() -> None:
    with TemporaryDirectory() as tmpdir:
        client = _client(_DataHubStub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3")))

        response = client.post(
            "/api/alerts",
            json={
                "symbol": "600519",
                "condition_type": "price_below",
                "threshold": 1200.0,
                "name": "   ",
            },
        )

    assert response.status_code == 200
    assert response.json()["name"] == "价格下破 1200"


def test_update_alert_route_rejects_unknown_fields() -> None:
    with TemporaryDirectory() as tmpdir:
        client = _client(_DataHubStub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3")))

        response = client.patch(
            "/api/alerts/1",
            json={
                "threshold": 1200.0,
                "condition_type": "price_below",
            },
        )

    assert response.status_code == 422


def test_update_alert_route_rejects_non_finite_threshold() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        quote = make_quote()
        created = cache.create_alert_rule(
            quote,
            AlertRuleInput(symbol="600519", condition_type="price_below", threshold=1200.0),
        )
        client = _client(_DataHubStub(cache=cache))

        response = client.patch(
            f"/api/alerts/{created.id}",
            json={"threshold": math.inf},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "body / threshold: 应为有效数字"}


def test_create_alert_route_renders_type_errors_in_chinese() -> None:
    with TemporaryDirectory() as tmpdir:
        client = _client(_DataHubStub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3")))

        response = client.post(
            "/api/alerts",
            json={
                "symbol": "600519",
                "condition_type": "price_below",
                "threshold": None,
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "body / threshold: 应为有效数字"}


def test_create_alert_route_rejects_non_finite_threshold() -> None:
    with TemporaryDirectory() as tmpdir:
        client = _client(_DataHubStub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3")))

        response = client.post(
            "/api/alerts",
            json={
                "symbol": "600519",
                "condition_type": "price_below",
                "threshold": math.nan,
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "body / threshold: 应为有效数字"}


def test_alert_rules_route_maps_sqlite_errors_to_api_detail() -> None:
    client = _client(_DataHubStub(cache=_FailingCache(sqlite3.OperationalError("database is locked"))))

    response = client.get("/api/alerts")

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}


def test_alert_events_route_maps_sqlite_errors_to_api_detail() -> None:
    client = _client(_DataHubStub(cache=_FailingCache(sqlite3.OperationalError("database is locked"))))

    response = client.get("/api/alerts/events")

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}


def test_delete_alert_rule_returns_404_when_rule_is_missing() -> None:
    client = _client(_DataHubStub(cache=_DeleteCache(removed=False)))

    response = client.delete("/api/alerts/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "预警规则不存在"}


def _client(datahub) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(alerts.router)
    app.dependency_overrides[get_datahub] = lambda: datahub
    return TestClient(app)


class _DataHubStub:
    def __init__(self, *, cache) -> None:
        self.cache = cache

    async def quote(self, symbol: str):
        return make_quote()


class _FailingCache:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def alert_rules(self, symbol: str | None = None, include_disabled: bool = True):
        raise self._exc

    def alert_events(self, symbol: str | None = None, limit: int = 100):
        raise self._exc


class _DeleteCache:
    def __init__(self, *, removed: bool) -> None:
        self._removed = removed

    def delete_alert_rule(self, rule_id: int) -> bool:
        return self._removed
