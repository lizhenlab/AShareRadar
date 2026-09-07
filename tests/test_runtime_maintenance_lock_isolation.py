from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, get_ident

import pytest

from app.config import Settings
from app.repositories import maintenance
from app.services.cache import SQLiteCache


@pytest.fixture
def cache(tmp_path: Path) -> SQLiteCache:
    path = tmp_path / "runtime.sqlite3"
    return SQLiteCache(path, settings=Settings(cache_path=path, max_cache_event_rows=1))


def _create_scan(cache: SQLiteCache) -> int:
    return cache.create_market_scan_run(
        trigger="manual", rule_version="test-lock-isolation", as_of="2026-09-07 10:00:00",
        data_date="2026-09-07", scope="test",
    ).id


@pytest.mark.parametrize("operation", ["cleanup_runtime_rows", "cleanup_regenerable_runtime_rows", "preview_runtime_cleanup"])
def test_artifact_deep_validation_does_not_block_scan_creation(cache, monkeypatch, operation) -> None:
    entered, release, written = Event(), Event(), Event()
    original = maintenance.market_scan_artifact_protection

    def paused_validation(path):
        entered.set()
        assert release.wait(timeout=5), "test did not release artifact validation"
        return original(path)

    def create_scan():
        run_id = _create_scan(cache)
        written.set()
        return run_id

    monkeypatch.setattr(maintenance, "market_scan_artifact_protection", paused_validation)
    with ThreadPoolExecutor(max_workers=2) as workers:
        cleanup = workers.submit(getattr(cache.maintenance_repo, operation))
        try:
            assert entered.wait(timeout=5), "maintenance did not reach artifact validation"
            scan = workers.submit(create_scan)
            assert written.wait(timeout=1), "artifact validation held the shared cache lock"
        finally:
            release.set()
        cleanup.result(timeout=5)
        run_id = scan.result(timeout=5)
    assert cache.market_scan_run(run_id).status == "queued"


def test_concurrent_periodic_cleanup_keeps_one_validation_and_one_interval(cache, monkeypatch) -> None:
    entered, release, second_started = Event(), Event(), Event()
    original = maintenance.market_scan_artifact_protection
    validations = []

    def paused_validation(path):
        validations.append(path)
        entered.set()
        assert release.wait(timeout=5)
        return original(path)

    def second_cleanup():
        second_started.set()
        return cache.maintenance_repo.cleanup_regenerable_runtime_rows()

    monkeypatch.setattr(maintenance, "market_scan_artifact_protection", paused_validation)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(cache.maintenance_repo.cleanup_regenerable_runtime_rows)
        try:
            assert entered.wait(timeout=5)
            second = workers.submit(second_cleanup)
            assert second_started.wait(timeout=5)
        finally:
            release.set()
        assert first.result(timeout=5)
        assert second.result(timeout=5) == {}
    assert validations == [cache.path]


def test_failed_validation_releases_locks_and_does_not_consume_cleanup_interval(cache, monkeypatch) -> None:
    original = maintenance.market_scan_artifact_protection
    validations = []

    def fails_once(path):
        validations.append(path)
        if len(validations) == 1:
            raise RuntimeError("injected artifact validation failure")
        return original(path)

    monkeypatch.setattr(maintenance, "market_scan_artifact_protection", fails_once)
    with pytest.raises(RuntimeError, match="injected artifact validation failure"):
        cache.maintenance_repo.cleanup_regenerable_runtime_rows()
    with ThreadPoolExecutor(max_workers=1) as workers:
        assert workers.submit(cache.maintenance_repo.cleanup_regenerable_runtime_rows).result(timeout=5)
    assert cache.maintenance_repo.cleanup_regenerable_runtime_rows() == {}
    assert validations == [cache.path, cache.path]


@pytest.mark.parametrize("paused_phase", ["validation", "compaction"])
def test_manual_transaction_waits_before_cache_lock_and_connection_borrowing(cache, monkeypatch, paused_phase) -> None:
    phase_entered, phase_release = Event(), Event()
    manual_waiting, manual_release, manual_entered = Event(), Event(), Event()
    abort_manual, written = Event(), Event()
    abort_manual.set()
    manual_thread = []
    repository = cache.maintenance_repo
    original_guard = repository.exclusive_operation
    target = maintenance if paused_phase == "validation" else repository
    method = "market_scan_artifact_protection" if paused_phase == "validation" else "_compact_after_cleanup"
    original_phase = getattr(target, method)

    def pause_phase(value):
        phase_entered.set()
        assert phase_release.wait(timeout=5), "test did not release maintenance phase"
        return original_phase(value)

    @contextmanager
    def pause_manual_guard():
        if manual_thread == [get_ident()] and not manual_waiting.is_set():
            manual_waiting.set()
            assert manual_release.wait(timeout=5), "test did not release manual operation"
            if abort_manual.is_set():
                raise RuntimeError("abort blocked manual operation after test failure")
        with original_guard():
            yield

    def manual_preview():
        manual_thread.append(get_ident())
        with cache.exclusive_local_data_operation() as operation, operation.transaction():
            manual_entered.set()
            return repository.preview_runtime_cleanup()

    def create_scan():
        run_id = _create_scan(cache)
        written.set()
        return run_id

    monkeypatch.setattr(target, method, pause_phase)
    monkeypatch.setattr(repository, "exclusive_operation", pause_manual_guard)
    with ThreadPoolExecutor(max_workers=3) as workers:
        cleanup = workers.submit(repository.cleanup_regenerable_runtime_rows)
        try:
            assert phase_entered.wait(timeout=5), "maintenance did not reach paused phase"
            manual = workers.submit(manual_preview)
            assert manual_waiting.wait(timeout=5), "manual operation did not request maintenance guard"
            scan = workers.submit(create_scan)
            assert written.wait(timeout=1), "manual maintenance waiter held the shared cache lock"
            abort_manual.clear()
            manual_release.set()
            assert not manual_entered.wait(timeout=0.1), "manual transaction borrowed connections during maintenance"
        finally:
            phase_release.set()
            manual_release.set()
        assert cleanup.result(timeout=5)
        assert manual.result(timeout=5)
        assert scan.result(timeout=5) > 0
    assert manual_entered.is_set()
