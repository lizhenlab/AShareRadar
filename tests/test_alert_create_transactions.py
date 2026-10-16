"""Alert write receipts must be constructed before their transaction commits."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import NoReturn

import pytest

from app.db.connection import SQLiteConnectionFactory
from app.models.user_data import AlertRuleInput
from app.repositories import alerts
from app.services.cache import SQLiteCache
from tests.factories import make_quote
from tests.test_api_alert_routes import _client, _DataHubStub


@pytest.mark.parametrize("fault", ["select", "mapping", "commit"])
def test_alert_create_failure_rolls_back_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    path = tmp_path / "alerts.sqlite3"
    cache = SQLiteCache(path)
    client = _client(_DataHubStub(cache=cache))
    payload = {"symbol": "600519", "condition_type": "price_below", "threshold": 1200.0}
    before = _stored_state(path)

    with monkeypatch.context() as scoped:
        _install_storage_fault(scoped, fault, "row_to_alert_rule")
        failed = client.post("/api/alerts", json=payload)

    assert failed.status_code == 503
    assert _stored_state(path) == before
    retried = client.post("/api/alerts", json=payload)
    assert retried.status_code == 200
    reopened = SQLiteCache(path).alert_rules()
    assert [item.model_dump(mode="json") for item in reopened] == [retried.json()]
    assert reopened[0].threshold == 1200.0


def test_alert_create_missing_readback_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "alerts.sqlite3"
    cache = SQLiteCache(path)
    before = _stored_state(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(alerts, "_alert_rule_row", lambda *_args: None)
        with pytest.raises(RuntimeError, match="预警规则保存失败"):
            cache.create_alert_rule(make_quote(), _payload())

    assert _stored_state(path) == before


@pytest.mark.parametrize("fault", ["select", "mapping", "commit"])
def test_alert_event_receipt_failure_rolls_back_state_and_event_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    path = tmp_path / "alerts.sqlite3"
    cache = SQLiteCache(path)
    rule = cache.create_alert_rule(make_quote(), _payload())
    before = _stored_state(path)

    with monkeypatch.context() as scoped:
        _install_storage_fault(scoped, fault, "row_to_alert_event")
        with pytest.raises((sqlite3.DatabaseError, RuntimeError)):
            _trigger(cache, rule)

    assert _stored_state(path) == before
    retried = _trigger(cache, rule)
    assert retried.applied and retried.event is not None
    reopened = SQLiteCache(path)
    assert reopened.alert_events() == [retried.event]
    updated = reopened.alert_rule(rule.id)
    assert updated is not None
    assert updated.last_state == "触发"
    assert updated.trigger_count == 1
    assert updated.last_triggered_at == retried.event.created_at
    assert not _trigger(cache, rule).applied
    assert reopened.alert_events() == [retried.event]


def test_alert_event_missing_readback_rolls_back_rule_state(tmp_path: Path) -> None:
    path = tmp_path / "alerts.sqlite3"
    cache = SQLiteCache(path)
    rule = cache.create_alert_rule(make_quote(), _payload())
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("""
            CREATE TRIGGER discard_inserted_alert_event AFTER INSERT ON alert_event
            BEGIN DELETE FROM alert_event WHERE id = NEW.id; END
        """)
    before = _stored_state(path)

    with pytest.raises(RuntimeError, match="预警事件保存失败"):
        _trigger(cache, rule)

    assert _stored_state(path) == before


def test_alert_state_without_event_still_commits_a_valid_receipt(tmp_path: Path) -> None:
    path = tmp_path / "alerts.sqlite3"
    cache = SQLiteCache(path)
    rule = cache.create_alert_rule(make_quote(), _payload())
    checked_at = "2026-10-15T01:30:00.000000Z"

    result = cache.update_alert_rule_state_checked(
        rule, checked_at=checked_at, state="未触发", triggered=False,
        message="未达到阈值", quote=make_quote(),
        decision=alerts.AlertStateDecision("恢复", False, False, 0),
    )

    assert result == alerts.AlertStateUpdateResult(applied=True)
    reopened = SQLiteCache(path)
    updated = reopened.alert_rule(rule.id)
    assert updated is not None
    assert updated.last_checked_at == checked_at
    assert updated.last_state == "未触发"
    assert updated.trigger_count == 0
    assert reopened.alert_events() == []


def _payload() -> AlertRuleInput:
    return AlertRuleInput(symbol="600519", condition_type="price_below", threshold=1200.0)


def _trigger(cache: SQLiteCache, rule) -> alerts.AlertStateUpdateResult:
    return cache.update_alert_rule_state_checked(
        rule, checked_at="2026-10-15T01:30:00.000000Z", state="触发", triggered=True,
        message="达到阈值", quote=make_quote(),
        decision=alerts.AlertStateDecision("触发", True, True, 1),
    )


def _stored_state(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with closing(sqlite3.connect(path)) as conn:
        return {
            "rules": conn.execute("SELECT * FROM alert_rule ORDER BY id").fetchall(),
            "events": conn.execute("SELECT * FROM alert_event ORDER BY id").fetchall(),
            "sequence": conn.execute(
                "SELECT * FROM sqlite_sequence WHERE name IN ('alert_rule', 'alert_event') ORDER BY name"
            ).fetchall(),
        }


def _install_storage_fault(monkeypatch: pytest.MonkeyPatch, fault: str, mapper: str) -> None:
    if fault == "mapping":
        def failed_mapping(_row: sqlite3.Row) -> NoReturn:
            raise RuntimeError("注入预警返回模型构建失败")

        monkeypatch.setattr(alerts, mapper, failed_mapping)
        return

    original = SQLiteConnectionFactory.connect

    @contextmanager
    def failing_connect(factory: SQLiteConnectionFactory) -> Iterator[sqlite3.Connection]:
        with original(factory) as conn:
            def authorize(action: int, arg1: str | None, *_args: object) -> int:
                denied = action == sqlite3.SQLITE_SELECT if fault == "select" else (
                    action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT"
                )
                return sqlite3.SQLITE_DENY if denied else sqlite3.SQLITE_OK

            conn.set_authorizer(authorize)
            yield conn

    monkeypatch.setattr(SQLiteConnectionFactory, "connect", failing_connect)
