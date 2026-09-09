from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import sqlite3
from threading import Event
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_domain_services
from app.api.routes import strategy_lab
from app.repositories.strategy_automation import StrategyAutomationIntegrityError, StrategyAutomationRepository
from app.utils.errors import NotFoundError
from tests.test_strategy_automation_atomic_completion import _isolated_environment


STAMP = "2026-09-10T02:45:00Z"


@pytest.fixture
def schedule_state(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config_settings._SHELL_ENV_VALUES", {})
    with _isolated_environment(tmp_path) as (cache, execution, strategy_id, run_id):
        app = FastAPI()
        app.include_router(strategy_lab.router)
        app.dependency_overrides[get_domain_services] = lambda: cache.domain_services
        with TestClient(app) as client:
            response = client.post("/api/strategy-lab/schedules", json={"strategy_id": strategy_id})
            assert response.status_code == 201
            yield client, cache, execution, strategy_id, run_id, response.json()["schedule_id"]


def _persisted_effects(cache):
    with sqlite3.connect(cache.path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("strategy_schedule_run", "strategy_execution", "strategy_alert_event")
        }


@pytest.mark.parametrize("mutation", ["disable", "archive"])
@pytest.mark.parametrize("retry", [False, True])
def test_committed_pause_or_archive_prevents_a_stale_poll_from_claiming(schedule_state, mutation, retry):
    client, cache, _execution, strategy_id, run_id, schedule_id = schedule_state
    service = cache.domain_services.strategy_automation
    if retry:
        assert service.repository.claim_run(schedule_id, run_id, timestamp=STAMP)
        service.repository.fail_run(schedule_id, run_id, error="retryable failure", timestamp=STAMP)
    before = _persisted_effects(cache)
    entered, release = Event(), Event()
    claim = service.repository.claim_run

    def delayed_claim(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return claim(*args, **kwargs)

    with patch.object(service.repository, "claim_run", side_effect=delayed_claim), ThreadPoolExecutor(max_workers=1) as workers:
        running = workers.submit(service.run_due)
        try:
            assert entered.wait(2)
            if mutation == "disable":
                response = client.patch(f"/api/strategy-lab/schedules/{schedule_id}", json={"enabled": False})
                assert response.status_code == 200 and not response.json()["enabled"]
            else:
                response = client.post(f"/api/strategy-lab/strategies/{strategy_id}/archive", json={
                    "expected_revision": 1, "archived": True,
                })
                assert response.status_code == 200 and response.json()["archived"]
        finally:
            release.set()
        result = running.result(timeout=5)
    assert result.checked_count == result.skipped_count == 1
    assert result.executed_count == result.failed_count == result.event_count == 0
    assert _persisted_effects(cache) == before
    if mutation == "disable":
        resumed = client.patch(f"/api/strategy-lab/schedules/{schedule_id}", json={"enabled": True})
        assert resumed.status_code == 200 and resumed.json()["enabled"]
        assert service.run_due().executed_count == 1
        assert service.run_due().skipped_count == 1


def test_pause_after_claim_does_not_cancel_an_already_owned_execution(schedule_state):
    client, cache, _execution, _strategy_id, run_id, schedule_id = schedule_state
    service = cache.domain_services.strategy_automation
    entered, release = Event(), Event()
    claim = service.repository.claim_run

    def admitted_claim(*args, **kwargs):
        result = claim(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    with patch.object(service.repository, "claim_run", side_effect=admitted_claim), ThreadPoolExecutor(max_workers=1) as workers:
        running = workers.submit(service.run_due)
        try:
            assert entered.wait(2)
            response = client.patch(f"/api/strategy-lab/schedules/{schedule_id}", json={"enabled": False})
            assert response.status_code == 200 and not response.json()["enabled"]
        finally:
            release.set()
        result = running.result(timeout=5)
    assert result.executed_count == 1 and result.failed_count == 0
    stored = service.repository.schedule(schedule_id)
    assert not stored.enabled and stored.last_market_scan_run_id == run_id
    assert stored.last_execution_id is not None
    assert service.run_due().checked_count == 0


def test_claim_admission_and_disable_are_serialized_across_database_connections(schedule_state):
    from app.repositories.strategy_automation import _require_schedule_strategy

    _client, cache, _execution, _strategy_id, run_id, schedule_id = schedule_state
    repository = cache.strategy_automation_repo
    other = StrategyAutomationRepository(cache.path)
    entered, release, disabling = Event(), Event(), Event()

    def pause_source_check(*args, **kwargs):
        _require_schedule_strategy(*args, **kwargs)
        entered.set()
        assert release.wait(5)

    def disable():
        disabling.set()
        return other.set_enabled(schedule_id, enabled=False, timestamp=STAMP)

    with patch("app.repositories.strategy_automation._require_schedule_strategy", side_effect=pause_source_check):
        with ThreadPoolExecutor(max_workers=2) as workers:
            claiming = workers.submit(repository.claim_run, schedule_id, run_id, timestamp=STAMP)
            try:
                assert entered.wait(2)
                writing = workers.submit(disable)
                assert disabling.wait(2)
                with pytest.raises(FutureTimeoutError):
                    writing.result(timeout=0.1)
            finally:
                release.set()
            assert claiming.result(timeout=5)
            assert not writing.result(timeout=5).enabled
    assert not repository.claim_run(schedule_id, run_id + 1, timestamp=STAMP)
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT market_scan_run_id, status FROM strategy_schedule_run").fetchall() == [(run_id, "running")]


@pytest.mark.parametrize("problem", ["fingerprint", "missing_version"])
def test_claim_rejects_invalid_pinned_source_without_writes(schedule_state, problem):
    _client, cache, _execution, strategy_id, run_id, schedule_id = schedule_state
    with sqlite3.connect(cache.path) as conn:
        if problem == "fingerprint":
            conn.execute("UPDATE strategy_schedule SET strategy_fingerprint = ? WHERE id = ?", ("0" * 64, schedule_id))
        else:
            conn.execute("DELETE FROM strategy_spec_version WHERE strategy_id = ?", (strategy_id,))
    expected = StrategyAutomationIntegrityError if problem == "fingerprint" else NotFoundError
    before = _persisted_effects(cache)
    with pytest.raises(expected):
        cache.strategy_automation_repo.claim_run(schedule_id, run_id, timestamp=STAMP)
    assert _persisted_effects(cache) == before


def test_claim_database_failure_propagates_without_partial_run(schedule_state):
    _client, cache, _execution, _strategy_id, run_id, schedule_id = schedule_state
    with sqlite3.connect(cache.path) as conn:
        conn.execute("""CREATE TRIGGER reject_claim AFTER INSERT ON strategy_schedule_run
            BEGIN SELECT RAISE(ABORT, 'synthetic claim failure'); END""")
    before = _persisted_effects(cache)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic claim failure"):
        cache.strategy_automation_repo.claim_run(schedule_id, run_id, timestamp=STAMP)
    assert _persisted_effects(cache) == before
