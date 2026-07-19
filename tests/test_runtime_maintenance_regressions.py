from __future__ import annotations

from pathlib import Path
import sqlite3

from app.config import Settings
from app.services.cache import SQLiteCache


def test_deep_market_scan_retry_chain_keeps_only_retained_leaf_and_direct_parent(tmp_path: Path) -> None:
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
        run = cache.prepare_market_scan_retry(run.id)
        chain.append(run)

    preview = cache.preview_runtime_cleanup()
    removed = cache.cleanup_runtime_rows()

    assert preview["market_scan_run"] == removed["market_scan_run"] == 4
    assert cache.table_counts()["market_scan_run"] == 2
    retained_leaf = cache.market_scan_run(chain[-1].id)
    assert retained_leaf.retry_of_run_id == chain[-2].id
    assert cache.market_scan_run(chain[-2].id).id == chain[-2].id
    with sqlite3.connect(path) as conn:
        remaining = [int(row[0]) for row in conn.execute("SELECT id FROM market_scan_run ORDER BY id")]
    assert remaining == [chain[-2].id, chain[-1].id]
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
