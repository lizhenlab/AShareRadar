from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from threading import Event
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_datahub
from app.api.routes import local_data
from app.config import Settings
from app.db.market_scan_integrity import (
    create_market_scan_immutability_triggers,
    drop_market_scan_immutability_triggers,
    market_scan_immutability_triggers_present,
    verify_market_scan_snapshot,
)
from app.services.cache import SQLiteCache, _BorrowedTransactionConnection
from tests.test_market_scan_retention import _insert_published_snapshot


def _cache(tmp_path: Path) -> SQLiteCache:
    path = tmp_path / "runtime.sqlite3"
    return SQLiteCache(
        path,
        settings=Settings(
            cache_path=path,
            scheduler_enabled=False,
            max_market_scan_runs=1,
            max_cache_event_rows=1,
        ),
    )


def _client(cache: SQLiteCache) -> TestClient:
    app = FastAPI()
    app.include_router(local_data.router)
    app.dependency_overrides[get_datahub] = lambda: SimpleNamespace(
        cache=cache, settings=cache.settings,
    )
    return TestClient(app, raise_server_exceptions=False)


def _scan_ids(cache: SQLiteCache, *, published: bool) -> tuple[int, int]:
    if published:
        with sqlite3.connect(cache.path) as conn:
            return _insert_published_snapshot(conn, 0), _insert_published_snapshot(conn, 1)
    ids = []
    for _ in range(2):
        run = cache.create_market_scan_run(
            trigger="manual", rule_version="retention-test-v1",
            as_of="2026-09-08 16:30:00", data_date="2026-09-08", scope="test",
        )
        cache.start_market_scan_run(run.id)
        cache.finish_market_scan_run(run.id, "failed", message="synthetic failure")
        ids.append(run.id)
    return ids[0], ids[1]


def _cache_events(cache: SQLiteCache, *, count: int = 2, payload_size: int = 10) -> None:
    with sqlite3.connect(cache.path) as conn:
        conn.executemany(
            "INSERT INTO cache_event(category, message, created_at) VALUES ('test', ?, ?)",
            [("x" * payload_size, "2026-09-08T00:00:00.000000Z") for _ in range(count)],
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _page_facts(cache: SQLiteCache) -> dict[str, int]:
    with sqlite3.connect(cache.path) as conn:
        return {
            "pages": conn.execute("PRAGMA page_count").fetchone()[0],
            "free": conn.execute("PRAGMA freelist_count").fetchone()[0],
            "size": cache.path.stat().st_size,
        }


def test_borrowed_transaction_state_tracks_the_real_connection() -> None:
    with sqlite3.connect(":memory:") as conn:
        borrowed = _BorrowedTransactionConnection(conn)
        assert borrowed.in_transaction is False
        conn.execute("BEGIN IMMEDIATE")
        assert borrowed.in_transaction is True
        borrowed.execute("BEGIN IMMEDIATE")
        assert borrowed.in_transaction is True
        conn.rollback()
        assert borrowed.in_transaction is False


@pytest.mark.parametrize("published", [False, True])
def test_cleanup_route_deletes_scan_overflow_in_owning_transaction(tmp_path, published) -> None:
    cache = _cache(tmp_path)
    oldest, latest = _scan_ids(cache, published=published)
    _cache_events(cache)
    with _client(cache) as client:
        preview = client.get("/api/local-data/cleanup-preview")
        response = client.post("/api/local-data/cleanup?confirm=retention-cleanup")
        repeated = client.post("/api/local-data/cleanup?confirm=retention-cleanup")

    assert preview.status_code == 200
    assert preview.json()["tables"]["market_scan_run"] == 1
    assert response.status_code == 200, response.text
    assert response.json()["tables"]["market_scan_run"] == 1
    assert response.json()["tables"]["cache_event"] == 1
    assert response.json()["committed"] is True
    assert repeated.status_code == 200 and repeated.json()["total_rows"] == 0
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT id FROM market_scan_run").fetchall() == [(latest,)]
        assert conn.execute("SELECT COUNT(*) FROM cache_event").fetchone()[0] == 1
        assert len(market_scan_immutability_triggers_present(conn)) == 5
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        if published:
            verify_market_scan_snapshot(conn, latest)
            assert conn.execute("SELECT COUNT(*) FROM market_scan_result WHERE run_id = ?", (oldest,)).fetchone()[0] == 0
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute("UPDATE market_scan_run SET message = 'blocked' WHERE id = ?", (latest,))


@pytest.mark.parametrize("published", [False, True])
def test_cleanup_route_delete_failure_rolls_back_every_table_without_compaction(tmp_path, monkeypatch, published) -> None:
    cache = _cache(tmp_path)
    oldest, latest = _scan_ids(cache, published=published)
    _cache_events(cache)
    with sqlite3.connect(cache.path) as conn:
        conn.execute(f"""
            CREATE TRIGGER injected_retention_failure BEFORE DELETE ON market_scan_run
            WHEN OLD.id = {oldest} BEGIN SELECT RAISE(ABORT, 'injected retention failure'); END
        """)
    compacted = []
    monkeypatch.setattr(cache.maintenance_repo, "_compact_database_if_worthwhile", lambda: compacted.append(True))

    with _client(cache) as client:
        response = client.post("/api/local-data/cleanup?confirm=retention-cleanup")

    assert response.status_code == 503
    assert "injected retention failure" in response.json()["detail"]
    assert compacted == []
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT id FROM market_scan_run ORDER BY id").fetchall() == [(oldest,), (latest,)]
        assert conn.execute("SELECT COUNT(*) FROM cache_event").fetchone()[0] == 2
        assert len(market_scan_immutability_triggers_present(conn)) == 5
        if published:
            verify_market_scan_snapshot(conn, oldest)
            verify_market_scan_snapshot(conn, latest)


def test_cleanup_route_still_rejects_tampered_seal_and_protects_referenced_run(tmp_path) -> None:
    cache = _cache(tmp_path)
    oldest, latest = _scan_ids(cache, published=True)
    _cache_events(cache)
    with sqlite3.connect(cache.path) as conn:
        drop_market_scan_immutability_triggers(conn)
        conn.execute("UPDATE market_scan_result SET score = 1 WHERE run_id = ?", (oldest,))
        create_market_scan_immutability_triggers(conn)
    with _client(cache) as client:
        corrupt = client.post("/api/local-data/cleanup?confirm=retention-cleanup")
    assert corrupt.status_code == 409
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache_event").fetchone()[0] == 2
        conn.execute("CREATE TABLE retained_reference (run_id INTEGER REFERENCES market_scan_run(id))")
        conn.execute("INSERT INTO retained_reference VALUES (?)", (oldest,))
    with _client(cache) as client:
        protected = client.post("/api/local-data/cleanup?confirm=retention-cleanup")
    assert protected.status_code == 200
    assert protected.json()["tables"]["market_scan_run"] == 0
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT id FROM market_scan_run ORDER BY id").fetchall() == [(oldest,), (latest,)]
        assert len(market_scan_immutability_triggers_present(conn)) == 5


def test_cleanup_route_compacts_only_after_real_commit_and_factory_restoration(tmp_path, monkeypatch) -> None:
    cache = _cache(tmp_path)
    _cache_events(cache, count=1000, payload_size=16_384)
    before = _page_facts(cache)
    repository = cache.maintenance_repo
    factory = repository._connections
    original = repository._compact_database_if_worthwhile
    observations = []

    def compact_after_commit():
        assert repository._connections is factory
        with sqlite3.connect(cache.path, timeout=0.05) as conn:
            assert conn.execute("SELECT COUNT(*) FROM cache_event").fetchone()[0] == 1
            conn.execute("BEGIN IMMEDIATE")
        observations.append("committed")
        return original()

    monkeypatch.setattr(repository, "_compact_database_if_worthwhile", compact_after_commit)
    with _client(cache) as client:
        response = client.post("/api/local-data/cleanup?confirm=retention-cleanup")
        after = _page_facts(cache)
        repeated = client.post("/api/local-data/cleanup?confirm=retention-cleanup")
    assert response.status_code == 200, response.text
    assert response.json()["committed"] is True
    assert response.json()["tables"]["cache_event"] == 999
    assert observations == ["committed"]
    assert before["size"] > 8 * 1024 * 1024
    assert after["size"] < before["size"] / 2
    assert after["pages"] < before["pages"] / 2 and after["free"] == 0
    assert repeated.status_code == 200 and repeated.json()["total_rows"] == 0
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_cleanup_route_optional_compaction_failure_keeps_committed_receipt(tmp_path, monkeypatch) -> None:
    cache = _cache(tmp_path)
    _cache_events(cache)
    repository = cache.maintenance_repo
    original_connect = repository._connect
    original_compact = repository._compact_database_if_worthwhile
    failures = []

    @contextmanager
    def failed_compaction_connection():
        failures.append("optional compaction unavailable")
        raise sqlite3.OperationalError("synthetic database is locked")
        yield  # pragma: no cover - expose a context manager failing on entry

    def compact_with_failure():
        repository._connect = failed_compaction_connection
        try:
            return original_compact()
        finally:
            repository._connect = original_connect

    monkeypatch.setattr(repository, "_compact_database_if_worthwhile", compact_with_failure)
    with _client(cache) as client:
        response = client.post("/api/local-data/cleanup?confirm=retention-cleanup")
        repeated = client.post("/api/local-data/cleanup?confirm=retention-cleanup")
    assert response.status_code == 200
    assert response.json()["committed"] is True
    assert response.json()["tables"]["cache_event"] == 1
    assert repeated.status_code == 200 and repeated.json()["total_rows"] == 0
    assert failures == ["optional compaction unavailable"]
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache_event").fetchone()[0] == 1


def test_cleanup_route_no_deletion_does_not_compact(tmp_path, monkeypatch) -> None:
    cache = _cache(tmp_path)
    compacted = []
    monkeypatch.setattr(cache.maintenance_repo, "_compact_database_if_worthwhile", lambda: compacted.append(True))
    with _client(cache) as client:
        response = client.post("/api/local-data/cleanup?confirm=retention-cleanup")
    assert response.status_code == 200
    assert response.json()["committed"] is True and response.json()["total_rows"] == 0
    assert compacted == []


def test_cleanup_route_commit_failure_rolls_back_and_never_compacts(tmp_path, monkeypatch) -> None:
    cache = _cache(tmp_path)
    _cache_events(cache)
    factory = cache.maintenance_repo._connections
    with sqlite3.connect(cache.path) as conn:
        conn.execute("CREATE TABLE retention_parent (id INTEGER PRIMARY KEY)")
        conn.execute("""
            CREATE TABLE retention_child (
                parent_id INTEGER REFERENCES retention_parent(id) DEFERRABLE INITIALLY DEFERRED
            )
        """)
    original = cache.cleanup_runtime_rows
    compacted = []

    def cleanup_with_deferred_failure(*, compact=True):
        removed = original(compact=compact)
        with cache.maintenance_repo._connect() as conn:
            conn.execute("INSERT INTO retention_child VALUES (999)")
        return removed

    monkeypatch.setattr(cache, "cleanup_runtime_rows", cleanup_with_deferred_failure)
    monkeypatch.setattr(cache.maintenance_repo, "_compact_database_if_worthwhile", lambda: compacted.append(True))
    with _client(cache) as client:
        response = client.post("/api/local-data/cleanup?confirm=retention-cleanup")
    assert response.status_code == 503
    assert "FOREIGN KEY constraint failed" in response.json()["detail"]
    assert compacted == [] and cache.maintenance_repo._connections is factory
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache_event").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM retention_child").fetchone()[0] == 0


def test_cleanup_route_compaction_releases_cache_lock_but_keeps_maintenance_guard(tmp_path, monkeypatch) -> None:
    cache = _cache(tmp_path)
    _cache_events(cache)
    entered, release, preview_started, preview_done = Event(), Event(), Event(), Event()

    def paused_compaction():
        entered.set()
        assert release.wait(timeout=5)
        return False

    def preview_cleanup():
        preview_started.set()
        result = cache.preview_runtime_cleanup()
        preview_done.set()
        return result

    monkeypatch.setattr(cache.maintenance_repo, "_compact_database_if_worthwhile", paused_compaction)
    with _client(cache) as client, ThreadPoolExecutor(max_workers=3) as workers:
        cleanup = workers.submit(client.post, "/api/local-data/cleanup?confirm=retention-cleanup")
        try:
            assert entered.wait(timeout=5)
            write = workers.submit(cache.start_task_run, "during optional compaction")
            assert write.result(timeout=1) > 0
            preview = workers.submit(preview_cleanup)
            assert preview_started.wait(timeout=1)
            assert not preview_done.wait(timeout=0.1)
        finally:
            release.set()
        assert cleanup.result(timeout=5).status_code == 200
        assert preview.result(timeout=5)["cache_event"] == 0
