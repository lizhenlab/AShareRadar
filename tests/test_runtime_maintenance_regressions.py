from __future__ import annotations

from pathlib import Path
import sqlite3
import threading

import pytest

from app.config import Settings
from app.services.cache import SQLiteCache
from app.repositories import maintenance
from app.repositories.runtime_research_artifact_retention import RuntimeCleanupIntegrityError


def _retention_cache(tmp_path: Path) -> SQLiteCache:
    path = tmp_path / "runtime.sqlite3"
    cache = SQLiteCache(path, settings=Settings(cache_path=path, max_cache_event_rows=1, max_market_scan_runs=1))
    with sqlite3.connect(path) as conn:
        conn.executemany("INSERT INTO cache_event(category,message,created_at) VALUES ('test',?,?)",
                         [(str(i), f"2026-09-23T00:00:0{i}Z") for i in range(3)])
    return cache


def _failed_runs(cache: SQLiteCache) -> list[int]:
    runs = []
    for _ in range(2):
        run = cache.create_market_scan_run(trigger="manual", rule_version="retention-test", as_of="2026-09-22 16:30:00",
                                           data_date="2026-09-22", scope="test")
        cache.start_market_scan_run(run.id)
        cache.finish_market_scan_run(run.id, "failed", message="test")
        runs.append(run.id)
    return runs


def test_periodic_cache_cleanup_without_scan_candidates_does_not_validate_unrelated_archives(tmp_path, monkeypatch) -> None:
    cache = _retention_cache(tmp_path)
    directory = tmp_path / "research/market_scan_probability_source"
    directory.mkdir(parents=True)
    (directory / "unrecognized.json").write_text("not an archive")
    original = maintenance.market_scan_artifact_protection
    validations = []
    def record(path):
        validations.append(path)
        return original(path)
    monkeypatch.setattr(maintenance, "market_scan_artifact_protection", record)
    removed = cache.maintenance_repo.cleanup_regenerable_runtime_rows()
    assert removed["cache_event"] == 2 and removed["market_scan_run"] == 0
    assert validations == []
    with pytest.raises(RuntimeCleanupIntegrityError):
        cache.preview_runtime_cleanup()
    assert validations == [cache.path]


def test_periodic_scan_deletion_still_requires_complete_artifact_validation(tmp_path) -> None:
    cache = _retention_cache(tmp_path)
    runs = _failed_runs(cache)
    directory = tmp_path / "research/market_scan_probability_source"
    directory.mkdir(parents=True)
    (directory / "unrecognized.json").write_text("not an archive")
    with pytest.raises(RuntimeCleanupIntegrityError):
        cache.maintenance_repo.cleanup_regenerable_runtime_rows()
    assert cache.table_counts()["cache_event"] == 3
    assert all(cache.market_scan_run(run_id) for run_id in runs)
    assert cache.maintenance_repo._last_regenerable_cleanup_at is None


def test_scan_candidate_appearing_after_preflight_rolls_back_and_retries_full_validation(tmp_path, monkeypatch) -> None:
    cache = _retention_cache(tmp_path)
    repository = cache.maintenance_repo
    original = repository._cleanup_artifact_protection
    inserted = []
    def insert_after_preflight(specs, *, only_verify_scan_deletions):
        result = original(specs, only_verify_scan_deletions=only_verify_scan_deletions)
        if not inserted:
            assert result is None
            inserted.extend(_failed_runs(cache))
        return result
    monkeypatch.setattr(repository, "_cleanup_artifact_protection", insert_after_preflight)
    with pytest.raises(RuntimeCleanupIntegrityError, match="预检后出现"):
        repository.cleanup_regenerable_runtime_rows()
    assert cache.table_counts()["cache_event"] == 3
    assert cache.table_counts()["market_scan_run"] == 2
    assert repository._last_regenerable_cleanup_at is None
    result = repository.cleanup_regenerable_runtime_rows()
    assert result["market_scan_run"] == 1 and result["cache_event"] == 2
    assert cache.market_scan_run(inserted[-1]).id == inserted[-1]


def test_deep_market_scan_retry_chain_keeps_complete_retained_lineage(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    cache = SQLiteCache(path, settings=Settings(cache_path=path, max_market_scan_runs=1))
    run = cache.create_market_scan_run(
        trigger="manual",
        rule_version="full-market-score-v1",
        as_of="2026-07-10 16:30:00",
        data_date="2026-07-10",
        scope="test",
    )
    chain = [run]
    for _index in range(6):
        cache.start_market_scan_run(run.id)
        cache.finish_market_scan_run(run.id, "failed", message="test")
        if len(chain) == 6:
            break
        run = cache.prepare_market_scan_retry(
            run.id,
            as_of="2026-07-10 16:45:00",
        )
        chain.append(run)

    preview = cache.preview_runtime_cleanup()
    removed = cache.cleanup_runtime_rows()

    assert preview["market_scan_run"] == removed["market_scan_run"] == 0
    assert cache.table_counts()["market_scan_run"] == len(chain)
    retained_leaf = cache.market_scan_run(chain[-1].id)
    assert retained_leaf.retry_of_run_id == chain[-2].id
    assert cache.market_scan_run(chain[-2].id).id == chain[-2].id
    with sqlite3.connect(path) as conn:
        remaining = [int(row[0]) for row in conn.execute("SELECT id FROM market_scan_run ORDER BY id")]
    assert remaining == [item.id for item in chain]
    assert cache.cleanup_runtime_rows()["market_scan_run"] == 0


def test_runtime_cleanup_compacts_database_when_free_page_budget_is_large(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    cache = SQLiteCache(path, settings=Settings(cache_path=path, max_cache_event_rows=1))
    payload = "x" * 16_384
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO cache_event (category, message, created_at) VALUES ('test', ?, ?)",
            [(payload, f"2026-07-19 12:{index // 60:02d}:{index % 60:02d}") for index in range(1_000)],
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size_before = path.stat().st_size
    monkeypatch.setattr("app.repositories.maintenance.DATABASE_COMPACTION_MIN_FREE_BYTES", 1)
    monkeypatch.setattr("app.repositories.maintenance.DATABASE_COMPACTION_MIN_FREE_RATIO", 0.01)

    removed = cache.cleanup_runtime_rows()

    size_after = path.stat().st_size
    assert removed["cache_event"] == 999
    assert size_before > 8 * 1024 * 1024
    assert size_after < size_before / 2
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert int(conn.execute("PRAGMA freelist_count").fetchone()[0]) < 10


def test_runtime_compaction_does_not_hold_shared_repository_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    cache = SQLiteCache(path, settings=Settings(cache_path=path, max_cache_event_rows=1))
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO cache_event (category, message, created_at) VALUES ('test', ?, ?)",
            [("event", f"2026-07-19 12:00:{index:02d}") for index in range(3)],
        )
    compaction_started = threading.Event()
    release_compaction = threading.Event()

    def block_compaction() -> bool:
        compaction_started.set()
        assert release_compaction.wait(timeout=2)
        return True

    monkeypatch.setattr(cache.maintenance_repo, "_compact_database_if_worthwhile", block_compaction)
    cleanup = threading.Thread(target=cache.cleanup_runtime_rows)
    cleanup.start()
    assert compaction_started.wait(timeout=1)

    presets = cache.domain_services.discovery.list_presets(page=1, page_size=20)

    release_compaction.set()
    cleanup.join(timeout=2)
    assert cleanup.is_alive() is False
    assert presets.items == []
