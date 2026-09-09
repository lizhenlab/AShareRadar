"""Schedule writes must check their pinned strategy inside the write transaction."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import sqlite3
from threading import Event
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_domain_services
from app.api.routes import strategy_lab
from app.config import Settings
from app.models.strategy_lab import StrategySpecArchiveRequest
from app.models.strategy_automation import StrategyScheduleCreate
from app.repositories.strategy_automation import StrategyAutomationIntegrityError
from app.services.cache import SQLiteCache
from app.utils.errors import NotFoundError


@pytest.fixture
def schedule_state(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config_settings._SHELL_ENV_VALUES", {})
    cache = SQLiteCache(settings=Settings(cache_path=tmp_path / "schedule.sqlite3", scheduler_enabled=False, llm_enabled=False))
    app = FastAPI()
    app.include_router(strategy_lab.router)
    app.dependency_overrides[get_domain_services] = lambda: cache.domain_services
    with TestClient(app) as client:
        response = client.post("/api/strategy-lab/strategies", json={"spec": {"name": "并发管理测试"}, "confirmed": True})
        assert response.status_code == 201
        yield client, cache, response.json()


def _schedule_mutation(client, strategy, action):
    payload = {"strategy_id": strategy["strategy_id"], "revision": strategy["revision"]}
    if action == "create":
        return "create_schedule", lambda: client.post("/api/strategy-lab/schedules", json=payload), None
    created = client.post("/api/strategy-lab/schedules", json=payload)
    assert created.status_code == 201
    schedule_id = created.json()["schedule_id"]
    endpoint = f"/api/strategy-lab/schedules/{schedule_id}"
    assert client.patch(endpoint, json={"enabled": False}).status_code == 200
    return "set_enabled", lambda: client.patch(endpoint, json={"enabled": True}), schedule_id


@pytest.mark.parametrize("action", ["create", "reenable"])
def test_archive_committed_after_service_check_rejects_http_schedule_write(schedule_state, action):
    client, cache, strategy = schedule_state
    method_name, mutate, schedule_id = _schedule_mutation(client, strategy, action)
    repository = cache.strategy_automation_repo
    original = getattr(repository, method_name)
    entered, release = Event(), Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    with patch.object(repository, method_name, side_effect=delayed), ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(mutate)
        try:
            assert entered.wait(2)
            archived = client.post(f"/api/strategy-lab/strategies/{strategy['strategy_id']}/archive", json={
                "expected_revision": strategy["revision"], "archived": True,
            })
            assert archived.status_code == 200
        finally:
            release.set()
        response = future.result(timeout=5)
    assert response.status_code == 400
    assert "已归档策略" in response.json()["detail"]
    if schedule_id is None:
        assert repository.schedules(strategy_id=None, include_disabled=True, page=1, page_size=20).total == 0
    else:
        assert not repository.schedule(schedule_id).enabled


@pytest.mark.parametrize("action", ["create", "reenable"])
def test_schedule_transaction_finishes_before_a_later_archive(schedule_state, action):
    from app.repositories.strategy_automation import _require_schedule_strategy

    client, cache, strategy = schedule_state
    other = SQLiteCache(settings=Settings(cache_path=cache.path, scheduler_enabled=False, llm_enabled=False))
    _method_name, mutate, _schedule_id = _schedule_mutation(client, strategy, action)
    entered, release, archive_started = Event(), Event(), Event()

    def pause_admitted(*args, **kwargs):
        _require_schedule_strategy(*args, **kwargs)
        entered.set()
        assert release.wait(5)

    def archive():
        archive_started.set()
        return other.domain_services.strategy_lab.archive(
            strategy["strategy_id"], StrategySpecArchiveRequest(expected_revision=1, archived=True),
        )

    with patch("app.repositories.strategy_automation._require_schedule_strategy", side_effect=pause_admitted):
        with ThreadPoolExecutor(max_workers=2) as workers:
            writing = workers.submit(mutate)
            try:
                assert entered.wait(2)
                archiving = workers.submit(archive)
                assert archive_started.wait(2)
                with pytest.raises(FutureTimeoutError):
                    archiving.result(timeout=0.1)
            finally:
                release.set()
            response = writing.result(timeout=5)
            assert archiving.result(timeout=5).archived
    assert response.status_code == (201 if action == "create" else 200)
    schedule = cache.strategy_automation_repo.schedule(response.json()["schedule_id"])
    assert schedule.enabled  # Archive retains the existing later scheduler-disable contract.
    denied = client.patch(f"/api/strategy-lab/schedules/{schedule.schedule_id}", json={"enabled": True})
    assert denied.status_code == 400


def _stored_schedule_rows(cache):
    with sqlite3.connect(cache.path) as conn:
        return conn.execute("SELECT * FROM strategy_schedule ORDER BY id").fetchall()


@pytest.mark.parametrize("action", ["create", "reenable"])
@pytest.mark.parametrize("failure", ["sqlite", "receipt"])
def test_schedule_mutation_failure_rolls_back_before_success_receipt(schedule_state, action, failure):
    client, cache, strategy = schedule_state
    method_name, mutate, _schedule_id = _schedule_mutation(client, strategy, action)
    original = getattr(cache.strategy_automation_repo, method_name)
    before = _stored_schedule_rows(cache)
    if failure == "sqlite":
        statement = "INSERT" if action == "create" else "UPDATE"
        with sqlite3.connect(cache.path) as conn:
            conn.execute(f"""CREATE TRIGGER reject_schedule AFTER {statement} ON strategy_schedule
                BEGIN SELECT RAISE(ABORT, 'synthetic schedule failure'); END""")
        response = mutate()
        assert response.status_code == 503
    else:
        # Enter after service prechecks, so the injected failure is the write's
        # own receipt mapper, not an independent preliminary read.
        def fail_write_receipt(*args, **kwargs):
            with patch("app.repositories.strategy_automation._schedule_from_row", side_effect=ValueError("synthetic receipt failure")):
                return original(*args, **kwargs)
        with patch.object(cache.strategy_automation_repo, method_name, side_effect=fail_write_receipt):
            response = mutate()
        assert response.status_code == 400
    assert _stored_schedule_rows(cache) == before


def test_current_head_updates_do_not_rebind_explicit_old_schedule_revision(schedule_state):
    client, cache, strategy = schedule_state
    new_spec = dict(strategy["spec"], hard_filters=[{"field": "amount", "operator": "gte", "value": 100_000_000.0}])
    updated = client.put(f"/api/strategy-lab/strategies/{strategy['strategy_id']}", json={
        "spec": new_spec, "expected_revision": 1, "confirmed": True,
    })
    assert updated.status_code == 200 and updated.json()["revision"] == 2
    assert updated.json()["fingerprint"] != strategy["fingerprint"]
    old = client.post("/api/strategy-lab/schedules", json={"strategy_id": strategy["strategy_id"], "revision": 1})
    latest = client.post("/api/strategy-lab/schedules", json={"strategy_id": strategy["strategy_id"]})
    assert old.status_code == latest.status_code == 201
    assert old.json()["strategy_version"] == 1 and old.json()["strategy_fingerprint"] == strategy["fingerprint"]
    assert latest.json()["strategy_version"] == 2 and latest.json()["strategy_fingerprint"] == updated.json()["fingerprint"]
    endpoint = f"/api/strategy-lab/schedules/{old.json()['schedule_id']}"
    assert client.patch(endpoint, json={"enabled": False}).status_code == 200
    resumed = client.patch(endpoint, json={"enabled": True})
    assert resumed.status_code == 200 and resumed.json()["strategy_version"] == 1
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM strategy_execution").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM strategy_schedule_run").fetchone()[0] == 0


@pytest.mark.parametrize("source_problem", ["missing_version", "invalid_spec"])
def test_disabling_does_not_require_a_readable_strategy_version(schedule_state, source_problem):
    client, cache, strategy = schedule_state
    response = client.post("/api/strategy-lab/schedules", json={"strategy_id": strategy["strategy_id"]})
    assert response.status_code == 201
    schedule_id = response.json()["schedule_id"]
    with sqlite3.connect(cache.path) as conn:
        if source_problem == "missing_version":
            conn.execute("DELETE FROM strategy_spec_version WHERE strategy_id = ?", (strategy["strategy_id"],))
        else:
            conn.execute("UPDATE strategy_spec_version SET spec_json = '{}' WHERE strategy_id = ?", (strategy["strategy_id"],))
    with patch.object(cache.domain_services.strategy_lab, "get", side_effect=AssertionError("disable must not load source")):
        disabled = client.patch(f"/api/strategy-lab/schedules/{schedule_id}", json={"enabled": False})
    assert disabled.status_code == 200
    assert not cache.strategy_automation_repo.schedule(schedule_id).enabled


@pytest.mark.parametrize("problem", ["missing_strategy", "missing_revision", "fingerprint"])
def test_repository_creation_rejects_missing_or_mismatched_pinned_source(schedule_state, problem):
    _client, cache, strategy = schedule_state
    strategy_id = strategy["strategy_id"] + 999 if problem == "missing_strategy" else strategy["strategy_id"]
    revision = 999 if problem == "missing_revision" else 1
    fingerprint = "0" * 64 if problem == "fingerprint" else strategy["fingerprint"]
    error = StrategyAutomationIntegrityError if problem == "fingerprint" else NotFoundError
    with pytest.raises(error):
        cache.strategy_automation_repo.create_schedule(
            StrategyScheduleCreate(strategy_id=strategy_id), revision=revision, fingerprint=fingerprint,
            timestamp="2026-09-09T08:00:00Z",
        )
    assert _stored_schedule_rows(cache) == []


def test_reenable_rejects_mismatched_pinned_fingerprint_but_disable_remains_available(schedule_state):
    client, cache, strategy = schedule_state
    created = client.post("/api/strategy-lab/schedules", json={"strategy_id": strategy["strategy_id"]})
    assert created.status_code == 201
    schedule_id = created.json()["schedule_id"]
    with sqlite3.connect(cache.path) as conn:
        conn.execute("UPDATE strategy_schedule SET enabled = 0, strategy_fingerprint = ? WHERE id = ?", ("0" * 64, schedule_id))
    before = _stored_schedule_rows(cache)
    rejected = client.patch(f"/api/strategy-lab/schedules/{schedule_id}", json={"enabled": True})
    assert rejected.status_code == 503 and "指纹" in rejected.json()["detail"]
    assert _stored_schedule_rows(cache) == before
    assert client.patch(f"/api/strategy-lab/schedules/{schedule_id}", json={"enabled": False}).status_code == 200
