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
from app.models.schemas import AlertEventItem, AlertRuleInput
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


def test_update_alert_route_accepts_condition_type_change() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        created = cache.create_alert_rule(
            make_quote(),
            AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1400.0),
        )
        client = _client(_DataHubStub(cache=cache))

        response = client.patch(
            f"/api/alerts/{created.id}",
            json={
                "threshold": 1200.0,
                "condition_type": "price_below",
            },
        )

    assert response.status_code == 200
    assert response.json()["condition_type"] == "price_below"
    assert response.json()["threshold"] == 1200.0


def test_update_alert_route_rejects_unknown_fields() -> None:
    with TemporaryDirectory() as tmpdir:
        client = _client(_DataHubStub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3")))

        response = client.patch("/api/alerts/1", json={"unsupported": True})

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
    cursor_response = client.get(
        "/api/alerts/events",
        params={"after_created_at": "2026-07-16 10:00:00", "after_id": 1},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}
    assert cursor_response.status_code == 503
    assert cursor_response.json() == {"detail": "本地数据库暂不可用：database is locked"}


def test_alert_events_cursor_paginates_ascending_by_database_id_with_legacy_parameters() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        events = _create_alert_events(cache)
        late_rule = cache.alert_rule(events[0].rule_id)
        assert late_rule is not None
        late_event = cache.update_alert_rule_state(
            late_rule,
            checked_at="2026-07-16 09:00:00",
            state="触发",
            triggered=True,
            message="晚提交的旧时间事件",
            quote=make_quote(),
            force_event=True,
        )
        assert late_event is not None
        client = _client(_DataHubStub(cache=cache))

        legacy = client.get("/api/alerts/events", params={"limit": 2})
        from_empty_baseline = client.get(
            "/api/alerts/events",
            params={"limit": 2, "after_created_at": "", "after_id": 0},
        )
        first_page = client.get(
            "/api/alerts/events",
            params={
                "limit": 2,
                "after_created_at": events[0].created_at,
                "after_id": events[0].id,
            },
        )
        second_page = client.get(
            "/api/alerts/events",
            params={
                "limit": 2,
                "after_created_at": events[2].created_at,
                "after_id": events[2].id,
            },
        )
        late_page = client.get(
            "/api/alerts/events",
            params={
                "limit": 2,
                "after_created_at": events[3].created_at,
                "after_id": events[3].id,
            },
        )

    assert legacy.status_code == 200
    assert [item["id"] for item in legacy.json()] == [late_event.id, events[3].id]
    assert [item["id"] for item in from_empty_baseline.json()] == [events[0].id, events[1].id]
    assert events[0].created_at == events[1].created_at
    assert [item["id"] for item in first_page.json()] == [events[1].id, events[2].id]
    assert [item["id"] for item in second_page.json()] == [events[3].id, late_event.id]
    assert [item["id"] for item in late_page.json()] == [late_event.id]


def test_alert_events_cursor_accepts_id_only_and_keeps_legacy_timestamp_validation() -> None:
    with TemporaryDirectory() as tmpdir:
        client = _client(_DataHubStub(cache=SQLiteCache(Path(tmpdir) / "cache.sqlite3")))

        id_only = client.get("/api/alerts/events", params={"after_id": 1})
        missing_id = client.get("/api/alerts/events", params={"after_created_at": "2026-07-16 10:00:00"})
        oversized = client.get("/api/alerts/events", params={"limit": 501})

    assert id_only.status_code == 200
    assert missing_id.status_code == 422
    assert oversized.status_code == 422


def test_create_alert_route_maps_sqlite_errors_from_async_write_to_api_detail() -> None:
    client = _client(_DataHubStub(cache=_CreateFailingCache()))

    response = client.post(
        "/api/alerts",
        json={
            "symbol": "600519",
            "condition_type": "price_below",
            "threshold": 1200.0,
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "本地数据库暂不可用：database is locked"}


def test_delete_alert_rule_returns_404_when_rule_is_missing() -> None:
    client = _client(_DataHubStub(cache=_DeleteCache(removed=False)))

    response = client.delete("/api/alerts/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "预警规则不存在"}


def test_delete_alert_rule_returns_mutation_result_when_removed() -> None:
    client = _client(_DataHubStub(cache=_DeleteCache(removed=True)))

    response = client.delete("/api/alerts/9")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "removed": True}


def test_delete_alert_rule_uses_mutation_response_model() -> None:
    app = FastAPI()
    app.include_router(alerts.router)

    schema = app.openapi()["paths"]["/api/alerts/{rule_id}"]["delete"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    assert schema == {"$ref": "#/components/schemas/MutationResult"}


def _client(datahub) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(alerts.router)
    app.dependency_overrides[get_datahub] = lambda: datahub
    return TestClient(app)


def _create_alert_events(cache: SQLiteCache) -> list[AlertEventItem]:
    quote = make_quote()
    rule = cache.create_alert_rule(
        quote,
        AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1200.0),
    )
    timestamps = (
        "2026-07-16 10:00:00",
        "2026-07-16 10:00:00",
        "2026-07-16 10:01:00",
        "2026-07-16 10:02:00",
    )
    events = []
    for index, checked_at in enumerate(timestamps, start=1):
        event = cache.update_alert_rule_state(
            rule,
            checked_at=checked_at,
            state="触发",
            triggered=True,
            message=f"测试触发 {index}",
            quote=quote,
            force_event=True,
        )
        assert event is not None
        events.append(event)
    return events


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

    def alert_events(
        self,
        symbol: str | None = None,
        limit: int = 100,
        *,
        after_created_at: str | None = None,
        after_id: int | None = None,
    ):
        raise self._exc


class _CreateFailingCache:
    def create_alert_rule(self, quote, payload):
        raise sqlite3.OperationalError("database is locked")


class _DeleteCache:
    def __init__(self, *, removed: bool) -> None:
        self._removed = removed

    def delete_alert_rule(self, rule_id: int) -> bool:
        return self._removed
